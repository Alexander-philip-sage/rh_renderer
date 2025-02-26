from .single_tile_affine_renderer import SingleTileAffineRenderer
import numpy as np
import tinyr
from enum import Enum
import asyncio

class BlendType(Enum):
    NO_BLENDING = 0
    AVERAGING = 1
    LINEAR = 2
    MULTI_BAND_SEAM = 3


class MultipleTilesAffineRenderer:
    BLEND_TYPE = {
            "NO_BLENDING" : 0,
            "AVERAGING" : 1,
            "LINEAR" : 2
        }

    def __init__(self, single_tiles, blend_type=BlendType.NO_BLENDING):
        """Receives a number of image paths, and for each a transformation matrix"""
        self.blend_type = blend_type
        self.single_tiles = single_tiles
        # Create an RTree of the bounding boxes of the tiles
        self.rtree = tinyr.RTree(interleaved=True, max_cap=5, min_cap=2)
        for t in self.single_tiles:
            bbox = t.get_bbox()
            # using the (x_min, y_min, x_max, y_max) notation
            self.rtree.insert(t, (bbox[0], bbox[2], bbox[1], bbox[3]))
        #should_compute_mask = False if self.blend_type == 0 else True
        #self.single_tiles = [SingleTileAffineRenderer(img_path, img_shape[1], img_shape[0], compute_mask=should_compute_mask) for img_path, img_shape in zip(img_paths, img_shapes)]
        #for i, matrix in enumerate(transform_matrices):
        #    self.single_tiles[i].add_transformation(matrix)

    def add_transformation(self, transform_matrix):
        """Adds a transformation to all tiles"""
        self.rtree = tinyr.RTree(interleaved=True, max_cap=5, min_cap=2)
        for single_tile in self.single_tiles:
            single_tile.add_transformation(transform_matrix)
            bbox = single_tile.get_bbox()
            # using the (x_min, y_min, x_max, y_max) notation
            self.rtree.insert(single_tile, (bbox[0], bbox[2], bbox[1], bbox[3]))
        
    async def async_cache(self, points):
        '''concurrent caching of all tiles in RAM
        load and cache only tiles that will be used'''
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
        if len(self.single_tiles) == 0:
            return None

        # Distinguish between the different types of blending
        if self.blend_type == BlendType.NO_BLENDING: # No blending
            res = np.zeros((int(round(to_y + 1 - from_y)), int(round(to_x + 1 - from_x))), dtype=np.uint8)
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
            res = np.zeros((int(round(to_y + 1 - from_y)), int(round(to_x + 1 - from_x))), dtype=np.uint16)
            res_mask = np.zeros((int(round(to_y + 1 - from_y)), int(round(to_x + 1 - from_x))), dtype=np.uint8)

            # render only relevant parts, and stitch them together
            # filter only relevant tiles using rtree
            rect_res = self.rtree.search( (from_x, from_y, to_x, to_y) )
            for t in rect_res:
                t_img, t_start_point, t_mask = t.crop(from_x, from_y, to_x, to_y)
                if t_img is not None:
                    t_rel_point = np.array([int(round(t_start_point[0] - from_x)), int(round(t_start_point[1] - from_y))], dtype=int)
                    res[t_rel_point[1]:t_rel_point[1] + t_img.shape[0],
                        t_rel_point[0]:t_rel_point[0] + t_img.shape[1]] += t_img
                    res_mask[t_rel_point[1]:t_rel_point[1] + t_img.shape[0],
                                t_rel_point[0]:t_rel_point[0] + t_img.shape[1]] += t_mask

            # Change the values of 0 in the mask to 1, to avoid division by 0
            res_mask[res_mask == 0] = 1
            res = res / res_mask
            res = res.astype(np.uint8)

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
                    t_rel_point = np.array([int(round(t_start_point[0] - from_x)), int(round(t_start_point[1] - from_y))], dtype=int)
                    res[t_rel_point[1]:t_rel_point[1] + t_img.shape[0],
                        t_rel_point[0]:t_rel_point[0] + t_img.shape[1]] += (t_img * t_weights).astype(np.uint32)
                    res_weights[t_rel_point[1]:t_rel_point[1] + t_img.shape[0],
                                t_rel_point[0]:t_rel_point[0] + t_img.shape[1]] += t_weights.astype(np.uint16)

            # Change the weights that are 0 to 1, to avoid division by 0
            res_weights[res_weights < 1] = 1
            res = res / res_weights
            res = res.astype(np.uint8)

        return res

