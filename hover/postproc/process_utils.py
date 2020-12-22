import cv2
import numpy as np

from scipy.ndimage import filters, measurements
from scipy.ndimage.morphology import (
    binary_dilation,
    binary_fill_holes,
    distance_transform_cdt,
    distance_transform_edt,
)

from skimage.morphology import remove_small_objects, watershed
from hover.misc.utils import get_bounding_box

import warnings


def noop(*args, **kargs):
    pass

warnings.warn = noop

def __proc_np_hv(pred):
    """Process Nuclei Prediction with XY Coordinate Map

    Args:
        pred: prediction output, assuming 
              channel 0 contain probability map of nuclei
              channel 1 containing the regressed X-map
              channel 2 containing the regressed Y-map
    
    Return:
        proced_map: instance map containing unique value for each nucleus

    """
    pred = np.array(pred, dtype=np.float32)

    blb_raw = pred[..., 0]
    h_dir_raw = pred[..., 1]
    v_dir_raw = pred[..., 2]

    # processing
    blb = np.array(blb_raw >= 0.5, dtype=np.int32)

    blb = measurements.label(blb)[0]
    blb = remove_small_objects(blb, min_size=10)
    blb[blb > 0] = 1  # background is 0 already

    h_dir = cv2.normalize(
        h_dir_raw, None, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_32F
    )
    v_dir = cv2.normalize(
        v_dir_raw, None, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_32F
    )

    sobelh = cv2.Sobel(h_dir, cv2.CV_64F, 1, 0, ksize=21)
    sobelv = cv2.Sobel(v_dir, cv2.CV_64F, 0, 1, ksize=21)

    sobelh = 1 - (
        cv2.normalize(
            sobelh, None, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_32F
        )
    )
    sobelv = 1 - (
        cv2.normalize(
            sobelv, None, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_32F
        )
    )

    overall = np.maximum(sobelh, sobelv)
    overall = overall - (1 - blb)
    overall[overall < 0] = 0

    dist = (1.0 - overall) * blb
    ## nuclei values form mountains so inverse to get basins
    dist = -cv2.GaussianBlur(dist, (3, 3), 0)

    overall = np.array(overall >= 0.4, dtype=np.int32)

    marker = blb - overall
    marker[marker < 0] = 0
    marker = binary_fill_holes(marker).astype("uint8")
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    marker = cv2.morphologyEx(marker, cv2.MORPH_OPEN, kernel)
    marker = measurements.label(marker)[0]
    marker = remove_small_objects(marker, min_size=10)

    proced_pred = watershed(dist, marker, mask=blb)

    return proced_pred


def process(pred_map, nr_types=None, return_dict=False, return_probs=False):
    """Post processing script for image tiles

    Args:
        pred_map: commbined output of tp, np and hv branches, in the same order
        nr_types: number of types considered at output of nc branch
        return_dict: whether to return the dictionary of instance results
        return_probs: whether to return the per class probabilities for each nucleus
    
    Returns:
        pred_inst: pixel-wise nuclear instance segmentation prediction
        inst_info_dict: dictionary of instance-level nuclear results

    """
    if nr_types is not None:
        pred_type = pred_map[..., :nr_types]
        pred_type = np.argmax(pred_type, axis=-1)
        pred_inst = pred_map[..., nr_types:]
        
    else:
        pred_inst = pred_map

    pred_inst = np.squeeze(pred_inst)
    pred_inst = __proc_np_hv(pred_inst)

    inst_info_dict = None
    if return_dict or nr_types is not None:
        inst_id_list = np.unique(pred_inst)[1:]  # exlcude background
        inst_info_dict = {}
        inst_bbox_dict = {}
        for idx, inst_id in enumerate(inst_id_list):
            inst_map = pred_inst == inst_id
            # TODO: change format of bbox output
            rmin, rmax, cmin, cmax = get_bounding_box(inst_map)
            inst_bbox = np.array([[rmin, cmin], [rmax, cmax]])
            inst_map = inst_map[
                inst_bbox[0][0] : inst_bbox[1][0], inst_bbox[0][1] : inst_bbox[1][1]
            ]
            inst_map = inst_map.astype(np.uint8)
            inst_moment = cv2.moments(inst_map)
            inst_contour = cv2.findContours(
                inst_map, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE
            )
            # * opencv protocol format may break
            inst_contour = np.squeeze(inst_contour[0][0].astype("int32"))
            inst_centroid = [
                (inst_moment["m10"] / inst_moment["m00"]),
                (inst_moment["m01"] / inst_moment["m00"]),
            ]
            inst_centroid = np.array(inst_centroid)
            inst_contour[:, 0] += inst_bbox[0][1]  # X
            inst_contour[:, 1] += inst_bbox[0][0]  # Y
            inst_centroid[0] += inst_bbox[0][1]  # X
            inst_centroid[1] += inst_bbox[0][0]  # Y
            inst_info_dict[inst_id] = {  # inst_id should start at 1
                "centroid": inst_centroid,
                "contour": inst_contour,
            }
            inst_bbox_dict[inst_id] = {  # inst_id should start at 1
                "bbox": inst_bbox
            }


    if nr_types is not None:
        #### * Get class of each instance id, stored at index id-1
        for idx, inst_id in enumerate(inst_id_list):
            rmin, cmin, rmax, cmax = (inst_bbox_dict[inst_id]["bbox"]).flatten()
            inst_map_crop = pred_inst[rmin:rmax, cmin:cmax]
            inst_type_crop = pred_type[rmin:rmax, cmin:cmax]
            inst_map_crop = inst_map_crop == inst_id
            inst_type = inst_type_crop[inst_map_crop]
            type_list_, type_pixels_ = np.unique(inst_type, return_counts=True)
            type_list = list(zip(type_list_, type_pixels_))
            type_list = sorted(type_list, key=lambda x: x[1], reverse=True)
            inst_type = type_list[0][0]
            if inst_type == 0:  # ! pick the 2nd most dominant if it exists
                if len(type_list) > 1:
                    inst_type = type_list[1][0]
            inst_info_dict[inst_id]["type"] = int(inst_type)
            if return_probs:
                type_list_ = type_list_.tolist()
                nr_pix = np.sum(inst_map_crop)
                inst_probs = np.zeros([nr_types])
                for type_idx in type_list_:
                    inst_probs[type_idx] = float(type_pixels_[type_list_.index(type_idx)] / nr_pix)
                inst_info_dict[inst_id]["probs"] = inst_probs

    return pred_inst, inst_info_dict
