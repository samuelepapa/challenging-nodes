import SimpleITK as sitk
import numpy as np
import scipy.ndimage as ndi
import SimpleITK
import warnings
from skimage.measure import regionprops
import cv2
import torch

CORRECTION = 255


def get_nodule_diameter(seg_image):
    seg_image = np.mean(seg_image, axis=1)
    seg_image[seg_image != 0] = 255
    seg_image = seg_image.astype(int)
    properties = regionprops(seg_image)

    for p in properties:
        min_row, min_col, max_row, max_col = p.bbox
        diameter = max(max_row - min_row, max_col - min_col)
        # print(diameter)

    return diameter


def get_nodule_bbox(seg_image: np.array):
    seg_image[seg_image != 0] = 255
    seg_image = seg_image.astype(int)
    properties = regionprops(seg_image)

    for p in properties:
        min_row, min_col, max_row, max_col = p.bbox
        diameter = max(max_row - min_row, max_col - min_col)
        # print(diameter)

    return diameter, min_row, min_col, max_row, max_col


def generate_2d(X_ct, p_lambda=0.85):
    """
    Generate 2D digitally reconstructed radiographs from CT scan. (DRR, fake CXR, simulated CXR)
    X_ct: CT scan
    p-lambda:  β controls the boosting of X-ray absorption as the tissue density increases.
    We have chosen β=0.85 for our experiments after performing a visual comparison with real chest X-rays.
    """
    X_ct[X_ct > 400] = 400
    X_ct[X_ct < -500] = -500
    X_ct += 1024
    # 1424 524 698.748232
    X_ct = X_ct / 1000.0
    X_ct *= p_lambda
    X_ct[X_ct > 1] = 1
    # 1.0 0.4454 0.5866707652
    X_ct_2d = np.mean(np.exp(X_ct), axis=1)
    return X_ct_2d


def resample(image, voxel_spacing, new_spacing=None, new_shape=None, order=1):
    """Resamples the scan according to the either new spacing or new shape
    When new_spacing and new_shape are provided, new_shape has the priority
    use order = 1 for nearest neighbor and order = 3 for cubic interpolation
    @author: Joris Bukala+ Gabriel Humpire
    """
    assert new_spacing is not None or new_shape is not None
    if (
        np.dtype(image[0, 0, 0]) is np.dtype(np.int16)
        and np.min(image) < 0
        and np.max(image) > 50
        and order == 1
    ):
        warnings.warn(
            "Order 1 selected for image that looks as a scan, try using order 3"
        )
    if (
        np.dtype(image[0, 0, 0]) in [np.dtype(np.uint8), np.dtype(np.int16)]
        and np.min(image) == 0
        and np.max(image) <= 50
        and order == 3
    ):
        warnings.warn(
            "Order 3 selected for image that looks as a reference mask, try using order 1"
        )

    if new_shape is not None:
        new_shape = np.array(new_shape)
        real_resize_factor = new_shape / image.shape
        new_spacing = voxel_spacing / real_resize_factor
    elif new_spacing is not None:
        if voxel_spacing[0] == voxel_spacing[1]:
            voxel_spacing = np.flipud(voxel_spacing)
        scan_sz_mm = [sz * voxel_spacing[idx] for idx, sz in enumerate(image.shape)]
        new_shape = [
            round(float(sz_mm) / float(new_spacing[idx]))
            for idx, sz_mm in enumerate(scan_sz_mm)
        ]
        new_shape = np.array(new_shape)
        real_resize_factor = new_shape / image.shape

    new_spacing = np.flipud(new_spacing)

    image = ndi.interpolation.zoom(
        image, real_resize_factor, mode="nearest", order=order
    )
    return image, new_spacing


def convert_to_range_0_1(image_data):
    """
    Normalize image to be between 0 and 1
        image_data: the image to normalize
    returns the normalized image
    """
    image_max, _ = torch.max(
        image_data.view(
            image_data.shape[0],
            image_data.shape[1] * image_data.shape[2] * image_data.shape[3],
        ),
        dim=1,
    )
    image_min, _ = torch.min(
        image_data.view(
            image_data.shape[0],
            image_data.shape[1] * image_data.shape[2] * image_data.shape[3],
        ),
        dim=1,
    )
    image_min = image_min.unsqueeze(1).unsqueeze(2).unsqueeze(3)
    image_max = image_max.unsqueeze(1).unsqueeze(2).unsqueeze(3)
    try:
        return (image_data - image_min) / (image_max - image_min)
    except:
        print("invalid value encounteered")
        return image_data


def contrast_matching(nodule_2d, lung_photo, nodule_pixels):
    """
    Contrast matching according to Litjens et al.
    With some additional clip to prevent negative values or 0.
     nodule_2d: intensities of the nodule
     lung_photo: intensities of this particular lung area
    returns c, but is clipped to 0.4 since low values made the nodules neigh
    invisible sometimes.
    """
    it = torch.mean(nodule_2d[nodule_pixels].flatten())

    # mean of the surrounding lung tissue
    ib = torch.mean(lung_photo.flatten())

    # determine contrast
    c = torch.log(it / ib)

    return max(0.4, c)


import numpy as np
import cv2
import scipy.sparse
from scipy.sparse.linalg import spsolve

from os import path


def laplacian_matrix(n, m):
    """Generate the Poisson matrix.
    Refer to:
    https://en.wikipedia.org/wiki/Discrete_Poisson_equation
    Note: it's the transpose of the wiki's matrix
    """
    mat_D = scipy.sparse.lil_matrix((m, m))
    mat_D.setdiag(-1, -1)
    mat_D.setdiag(4)
    mat_D.setdiag(-1, 1)

    mat_A = scipy.sparse.block_diag([mat_D] * n).tolil()

    mat_A.setdiag(-1, 1 * m)
    mat_A.setdiag(-1, -1 * m)

    return mat_A


def poisson_edit(source, target, mask, offset):
    """The poisson blending function.
    Refer to:
    Perez et. al., "Poisson Image Editing", 2003.
    """

    # Assume:
    # target is not smaller than source.
    # shape of mask is same as shape of target.
    y_max, x_max = target.shape[:-1]
    y_min, x_min = 0, 0

    x_range = x_max - x_min
    y_range = y_max - y_min

    M = np.float32([[1, 0, offset[0]], [0, 1, offset[1]]])
    source = cv2.warpAffine(source, M, (x_range, y_range))
    source = np.expand_dims(source, axis=2)
    mask = mask[y_min:y_max, x_min:x_max]
    mask[mask != 0] = 1
    # mask = cv2.threshold(mask, 127, 1, cv2.THRESH_BINARY)

    mat_A = laplacian_matrix(y_range, x_range)

    # for \Delta g
    laplacian = mat_A.tocsc()

    # set the region outside the mask to identity
    for y in range(1, y_range - 1):
        for x in range(1, x_range - 1):
            if mask[y, x] == 0:
                k = x + y * x_range
                mat_A[k, k] = 1
                mat_A[k, k + 1] = 0
                mat_A[k, k - 1] = 0
                mat_A[k, k + x_range] = 0
                mat_A[k, k - x_range] = 0

    # corners
    # mask[0, 0]
    # mask[0, y_range-1]
    # mask[x_range-1, 0]
    # mask[x_range-1, y_range-1]

    mat_A = mat_A.tocsc()

    mask_flat = mask.flatten()
    for channel in range(source.shape[2]):
        source_flat = source[y_min:y_max, x_min:x_max, channel].flatten()
        target_flat = target[y_min:y_max, x_min:x_max, channel].flatten()

        # concat = source_flat*mask_flat + target_flat*(1-mask_flat)

        # inside the mask:
        # \Delta f = div v = \Delta g
        alpha = 1
        mat_b = laplacian.dot(source_flat) * alpha

        # outside the mask:
        # f = t
        mat_b[mask_flat == 0] = target_flat[mask_flat == 0]

        x = spsolve(mat_A, mat_b)
        # print(x.shape)
        x = x.reshape((y_range, x_range))
        # print(x.shape)
        # x[x > 255] = 255
        # x[x < 0] = 0
        # x = x.astype('uint8')
        # x = cv2.normalize(x, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX)
        # print(x.shape)

        target[y_min:y_max, x_min:x_max, channel] = x

    return target


def poisson_blend(nodule, lung_photo, x0, x1, y0, y1):
    """
    Poisson blend the nodule into the selected lung
        nodule: the nodule to blend into the lung picture
        lung_photo: the photo the nodule is going to be placed in
        x0: coordinate of left part of the bounding box where nodule is placed
        x1: coordinate of right part of the bounding box where nodule is placed
        y0: coordinate of upper part of the bounding box where nodule is placed
        y1: coordinate of lower part of the bounding box where nodule is placed
    returns the blended version of the two images
    """
    try:
        im = lung_photo
        center = (int(np.round((x1 + x0) / 2)), int(np.round((y1 + y0) / 2)))

        # determine the smallest box that can be drawn around the nodule pixels
        non_zero = np.argwhere(nodule)
        top_left = non_zero.min(axis=0)
        bottom_right = non_zero.max(axis=0)
        nodule = nodule[
            top_left[0] : bottom_right[0] + 1, top_left[1] : bottom_right[1] + 1
        ]

        obj = nodule

        # convert np to cv2
        cv2.imwrite("test_img.jpg", im * 255)
        cv2.imwrite("test_obj.jpg", obj * 255)
        im2 = cv2.imread("test_img.jpg")
        obj2 = cv2.imread("test_obj.jpg")

        # add gaussian blurring to reduce artefacts
        mask_blur = cv2.GaussianBlur(nodule, (5, 5), 0)
        # print('max min of obj2',np.max(obj2), np.min(obj2))
        cv2.imwrite("test_obj_masked2.jpg", obj2 / 255)

        # apply correction mask
        mask2 = np.ones(obj2.shape, obj2.dtype) * CORRECTION
        test_obj2 = cv2.imread("test_obj_masked2.jpg")

        # Poisson blend the images
        mixed_clone2 = cv2.seamlessClone(obj2, im2, mask2, center, cv2.MIXED_CLONE)
        return cv2.cvtColor(mixed_clone2, cv2.COLOR_BGR2GRAY)

    except Exception as e:
        print(e)
        print("there is a problem with cv2 poisson blending op")
        return np.array(lung_photo).squeeze()


def process_CT_patches(ct_path, seg_path, required_diameter):
    """
    Resample ct nodule patches and generates fake CXR nodule patches.
    """
    ct_image = SimpleITK.GetArrayFromImage(SimpleITK.ReadImage(ct_path))
    seg_img = SimpleITK.GetArrayFromImage(SimpleITK.ReadImage(seg_path))
    diameter = get_nodule_diameter(seg_img)
    scaling_factor = diameter / required_diameter

    image_resampled, new_spacing = resample(
        ct_image,
        voxel_spacing=[1, 1, 1],
        new_spacing=[scaling_factor, scaling_factor, scaling_factor],
    )
    seg_image_resampled, new_spacing = resample(
        seg_img,
        voxel_spacing=[1, 1, 1],
        new_spacing=[scaling_factor, scaling_factor, scaling_factor],
    )
    # put black values to the ct patch outside nodules.
    image_resampled[seg_image_resampled <= np.min(seg_image_resampled)] = np.min(
        image_resampled
    )
    # generate 2D digitially reconstructed CXR.
    X_ct_2d_resampled = generate_2d(image_resampled)
    # X_ct_2d_resampled[seg_image_resampled<=np.min(seg_image_resampled)]=np.min(X_ct_2d_resampled)

    return X_ct_2d_resampled, diameter
