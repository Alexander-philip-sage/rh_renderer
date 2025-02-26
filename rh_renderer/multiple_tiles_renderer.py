from __future__ import print_function
from .single_tile_renderer import AlphaTileRenderer
import numpy as np
import tinyr
import cv2
import time
from enum import Enum
import pyximport
pyximport.install()
from rh_renderer.blender.images_composer import PyImagesComposer
import asyncio

class BlendType(Enum):
    NO_BLENDING = 0
    AVERAGING = 1
    LINEAR = 2
    MULTI_BAND_SEAM = 3

class MultipleTilesRenderer:
    BLEND_TYPE = {
            "NO_BLENDING" : 0,
            "AVERAGING" : 1,
            "LINEAR" : 2
        }

    def __init__(self, single_tiles, blend_type=BlendType.NO_BLENDING, dtype=np.uint8):
        """Receives a number of image paths, and for each a transformation matrix"""
        self.blend_type = blend_type
        self.single_tiles = single_tiles
        # Create an RTree of the bounding boxes of the tiles
        self.rtree = tinyr.RTree(interleaved=True, max_cap=5, min_cap=2)
        self.dtype = dtype
        for t in self.single_tiles:
            bbox = t.get_bbox()
            # using the (x_min, y_min, x_max, y_max) notation
            self.rtree.insert(t, (bbox[0], bbox[2], bbox[1], bbox[3]))
        #should_compute_mask = False if self.blend_type == 0 else True
        #for i, matrix in enumerate(transform_matrices):
        #    self.single_tiles[i].add_transformation(matrix)

    def add_transformation(self, model):
        """Adds a transformation to all tiles"""
        self.rtree = tinyr.RTree(interleaved=True, max_cap=5, min_cap=2)
        for single_tile in self.single_tiles:
            single_tile.add_transformation(model)
            bbox = single_tile.get_bbox()
            # using the (x_min, y_min, x_max, y_max) notation
            self.rtree.insert(single_tile, (bbox[0], bbox[2], bbox[1], bbox[3]))
    async def async_cache(self, points):
        '''concurrent caching of all tiles in RAM
        load and cache only tiles that will be used
        Each point consists of a (from_x, from_y, to_x, to_y) for rtree.search'''
        if len(self.single_tiles) == 0:
            return
        # filter only relevant tiles using rtree
        ##this is loading the same tile twice sometimes since its a list instead of creating a set
        tiles_to_load = []
        unique_tiles = set()
        for pt in points:
            rect_res = self.rtree.search( pt )
            for t in rect_res:
                if t.img_path not in unique_tiles:
                    tiles_to_load.append(t)
                    unique_tiles.add(t.img_path)
        await asyncio.gather(*[t.async_cache() for t in tiles_to_load])

        
    def render(self):
        if len(self.single_tiles) == 0:
            return None, None

        # Render all tiles by finding the bounding box, and using crop
        all_bboxes = np.array([t.get_bbox() for t in self.single_tiles]).T
        bbox = [np.min(all_bboxes[0]), np.max(all_bboxes[1]), np.min(all_bboxes[2]), np.max(all_bboxes[3])]
        crop, start_point = self.crop(bbox[0], bbox[2], bbox[1], bbox[3])
        return crop, start_point

    def crop(self, from_x, from_y, to_x, to_y):
        print("multiple_tiles_renderer.crop. from_xy: {},{}, to_xy: {},{}".format(from_x, from_y, to_x, to_y))
        if len(self.single_tiles) == 0:
            return None, None

        # Distinguish between the different types of blending
        if self.blend_type == BlendType.NO_BLENDING: # No blending
            res = np.zeros((round(to_y + 1 - from_y), round(to_x + 1 - from_x)), dtype=self.dtype)
            # render only relevant parts, and stitch them together
            # filter only relevant tiles using rtree
            rect_res = self.rtree.search( (from_x, from_y, to_x, to_y) )
            for t in rect_res:
                t_img, t_start_point, _ = t.crop(from_x, from_y, to_x, to_y)
                if t_img is not None:
                    t_rel_point = np.array([int(round(t_start_point[0] - from_x)), int(round(t_start_point[1] - from_y))], dtype=int)
                    res[t_rel_point[1]:t_rel_point[1] + t_img.shape[0],
                        t_rel_point[0]:t_rel_point[0] + t_img.shape[1]] = t_img

        elif self.blend_type == BlendType.AVERAGING: # Averaging
            # Do the calculation on a uint16 image (for overlapping areas), and convert to uint8 at the end
            res = np.zeros(
                (int(round(to_y + 1 - from_y)), int(round(to_x + 1 - from_x))), 
                np.int32)
            res_mask = np.zeros(
                (int(round(to_y + 1 - from_y)), int(round(to_x + 1 - from_x))),
                np.int16)

            # render only relevant parts, and stitch them together
            # filter only relevant tiles using rtree
            rect_res = self.rtree.search( (from_x, from_y, to_x, to_y) )
            for t in rect_res:
                t_img, t_start_point, t_mask = t.crop(from_x, from_y, to_x, to_y)
                if t_img is not None:
                    #t_mask, _, _ = AlphaTileRenderer(t).crop(
                    #    from_x, from_y, to_x, to_y)
                    t_rel_point = np.array([int(round(t_start_point[0] - from_x)), int(round(t_start_point[1] - from_y))], dtype=int)
                    res[t_rel_point[1]:t_rel_point[1] + t_img.shape[0],
                        t_rel_point[0]:t_rel_point[0] + t_img.shape[1]] += t_img
                    res_mask[t_rel_point[1]:t_rel_point[1] + t_img.shape[0],
                                t_rel_point[0]:t_rel_point[0] + t_img.shape[1]] += t_mask

            # Change the values of 0 in the mask to 1, to avoid division by 0
            res_mask[res_mask == 0] = 1
            res = res / res_mask
            res = np.maximum(0, np.minimum(np.iinfo(self.dtype).max, res)).astype(self.dtype)

        elif self.blend_type == BlendType.LINEAR: # Linear averaging
            # Do the calculation on a uint32 image (for overlapping areas), and convert to uint8 at the end
            # For each pixel use the min-distance to an edge as a weight, and store the
            # average the outcome according to the weight
            res = np.zeros((int(round(to_y + 1 - from_y)), int(round(to_x + 1 - from_x))), dtype=np.uint32)
            res_weights = np.zeros((int(round(to_y + 1 - from_y)), int(round(to_x + 1 - from_x))), dtype=np.uint16)

            # render only relevant parts, and stitch them together
            # filter only relevant tiles using rtree
            rect_res = self.rtree.search( (from_x, from_y, to_x, to_y) )
            for t in rect_res:
                t_img, t_start_point, t_weights = t.crop_with_distances(from_x, from_y, to_x, to_y)
                if t_img is not None:
                    print("actual image start_point:", t_start_point, "and shape:", t_img.shape)
                    t_rel_point = np.array([int(round(t_start_point[0] - from_x)), int(round(t_start_point[1] - from_y))], dtype=int)
                    res[t_rel_point[1]:t_rel_point[1] + t_img.shape[0],
                        t_rel_point[0]:t_rel_point[0] + t_img.shape[1]] += (t_img * t_weights).astype(res.dtype)
                    res_weights[t_rel_point[1]:t_rel_point[1] + t_img.shape[0],
                                t_rel_point[0]:t_rel_point[0] + t_img.shape[1]] += t_weights.astype(res_weights.dtype)

            # Change the weights that are 0 to 1, to avoid division by 0
            res_weights[res_weights < 1] = 1
            res = res / res_weights
            res = np.maximum(0, np.minimum(np.iinfo(self.dtype).max, res)).astype(self.dtype)

        elif self.blend_type == BlendType.MULTI_BAND_SEAM: # multi-band with seam blending

            images = []
            images_masks = []
            images_corners = []
            rel_points = []
            seam_scale = 0.1
            images_seams = []
            images_seams_masks = []
            images_seams_corners = []
            min_rel_xy = np.array([np.iinfo(np.int32).max, np.iinfo(np.int32).max])
            st_time = time.time()
            # render only relevant parts, and stitch them together
            # filter only relevant tiles using rtree
            rect_res = self.rtree.search( (from_x, from_y, to_x, to_y) )
            for t in rect_res:
                t_img, t_start_point, t_mask = t.crop(from_x, from_y, to_x, to_y)
                if t_img is not None and t_img.shape[0] > 0 and t_img.shape[1] > 0:
                    # the relative start_point of t_img in the output image
                    print("actual image start_point:", t_start_point, "and shape:", t_img.shape)
                    t_rel_point = np.array([int(round(t_start_point[0] - from_x)), int(round(t_start_point[1] - from_y))], dtype=int)
                    min_rel_xy = np.minimum(min_rel_xy, t_rel_point)

                    # Change t_mask from 1 to 255 (keep 0's as 0)
                    t_mask[t_mask > 0] = 255

                    # TODO: change the composer to support non-rgb images
                    images.append(np.ascontiguousarray(t_img))
                    images_masks.append(np.ascontiguousarray(t_mask))
                    images_corners.append(np.ascontiguousarray(t_rel_point))
                    rel_points.append(t_rel_point)
            print("Rendering tiles ({} images) time: {}".format(len(rel_points), time.time() - st_time))

            # TODO - find the optimal seam scale
            #min_height_width = np.min([img.shape for img in images])
            #if min_height_width < 50:
            #    seam_scale = 1.0
            seam_scale = 1.0

            # Create the mipmaps for the seams
            st_time = time.time()
            if seam_scale == 1.0:
                images_seams = images
                images_seams_masks = images_masks
                images_seams_corners = rel_points
            else:
                for t_img, t_mask, t_rel_point in zip(images, images_masks, rel_points):

                        # Create downsampled images for seams calculation
                        #print("t_img shape: {}".format(t_img.shape))
                        t_img_seams = cv2.resize(t_img, (0, 0), fx=seam_scale, fy=seam_scale, interpolation=cv2.INTER_LINEAR)
                        t_mask_seams = cv2.resize(t_mask, (0, 0), fx=seam_scale, fy=seam_scale, interpolation=cv2.INTER_NEAREST)
                        #t_img_seams = cv2.resize(t_img_rgb, (0, 0), fx=seam_scale, fy=seam_scale, interpolation=cv2.INTER_AREA)
                        #t_mask_seams = cv2.resize(t_mask, (0, 0), fx=seam_scale, fy=seam_scale, interpolation=cv2.INTER_AREA)
                        t_seams_corner = (t_rel_point * seam_scale).astype(int)

    #                     t_img_seams = t_img.copy()
    #                     t_mask_seams = t_mask.copy()
    #                     t_seams_corner = (t_rel_point * seam_scale).astype(int)

                        images_seams.append(np.ascontiguousarray(t_img_seams))
                        images_seams_masks.append(np.ascontiguousarray(t_mask_seams))
                        images_seams_corners.append(np.ascontiguousarray(t_seams_corner))

            # Compose the blended image into a single image
            #cv2.imwrite('test1.jpg', images[0])
            #cv2.imwrite('test1_seams.jpg', images_seams[0])
            #print("len(images)", len(images))
            create_panorama = True
            if len(images) == 0:
                create_panorama = False

            if create_panorama:
                non_padded_res = PyImagesComposer.compose_panorama(images, images_masks, images_corners, seam_scale,
                                                        images_seams, images_seams_masks, images_seams_corners)
            images = None
            images_masks = None
            images_corners = None
            images_seams = None
            images_seams_masks = None
            images_seams_corners = None

            res = np.zeros((int(round(to_y + 1 - from_y)), int(round(to_x + 1 - from_x))), dtype=self.dtype)
            if create_panorama:
                res[min_rel_xy[1]:min_rel_xy[1] + non_padded_res.shape[0],
                    min_rel_xy[0]:min_rel_xy[0] + non_padded_res.shape[1]] = non_padded_res
            print("Blending tiles time: {}".format(time.time() - st_time))

        return res, (from_x, from_y)

