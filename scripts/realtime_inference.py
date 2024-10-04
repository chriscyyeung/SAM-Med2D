"""
Implements an OpenIGTLink client that expect pyigtl.ImageMessage and returns pyigtl.ImageMessage with YOLOv5 inference added to the image.
Arguments:
    model: string path to the torchscript file you intend to use
    input device name: This is the device name the client is listening to
    output device name: The device name the client outputs to
    host: the server's IP the client connects to.
    input port: port used for receiving data from the PLUS server over OpenIGTLink
    output port: port used for sending data to Slicer over OpenIGTLink
    target size: target quadratic size the model resizes to internally for predictions. Does not affect the actual output size
    confidence threshold: only bounding boxes above the given threshold will be visualized.
    line thickness: line thickness of drawn bounding boxes. Also affects font size of class names and confidence
"""
import sys
sys.path.append(".")

import argparse
import logging
import yaml
import time
import cv2
import numpy as np
import pyigtl
import torch
from pathlib import Path
from torch.nn import functional as F
from scipy.ndimage import map_coordinates
from scipy.spatial import Delaunay

from segment_anything import sam_model_registry

ROOT = Path(__file__).parent.resolve()


# Parse command line arguments
def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sam-checkpoint", type=str, help="sam checkpoint")
    parser.add_argument("--model-type", type=str, default="vit_b", help="sam model_type")
    parser.add_argument('--device', type=str, default='cuda')
    parser.add_argument("--image-size", type=int, default=256, help="image_size")
    parser.add_argument("--multimask", type=bool, default=True, help="output multimask")
    parser.add_argument("--encoder_adapter", type=bool, default=True, help="use adapter")
    parser.add_argument("--scanconversion-config", type=str, help="scan conversion yaml")
    parser.add_argument("--input-device-name", type=str, default="Image_Image")
    parser.add_argument("--output-device-name", type=str, default="Prediction")
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--input-port", type=int, default=18944)
    parser.add_argument("--output-port", type=int, default=18945)
    parser.add_argument("--log_file", type=str, default=None, help="log file")
    return parser.parse_args()


def scan_conversion_inverse(scanconversion_config):
    """
    Compute cartesian coordianates for inverse scan conversion.
    Mapping from curvilinear image to a rectancular image of scan lines as columns.
    The returned cartesian coordinates can be used to map the curvilinear image to a rectangular image using scipy.ndimage.map_coordinates.

    Args:
        scanconversion_config (dict): Dictionary with scan conversion parameters.

    Rerturns:
        x_cart (np.ndarray): x coordinates of the cartesian grid.
        y_cart (np.ndarray): y coordinates of the cartesian grid.

    Example:
        >>> x_cart, y_cart = scan_conversion_inverse(scanconversion_config)
        >>> scan_converted_image = map_coordinates(ultrasound_data[0, :, :, 0], [x_cart, y_cart], order=3, mode="nearest")
        >>> scan_converted_segmentation = map_coordinates(segmentation_data[0, :, :, 0], [x_cart, y_cart], order=0, mode="nearest")
    """

    # Create sampling points in polar coordinates
    initial_radius = np.deg2rad(scanconversion_config["angle_min_degrees"])
    final_radius = np.deg2rad(scanconversion_config["angle_max_degrees"])
    radius_start_px = scanconversion_config["radius_start_pixels"]
    radius_end_px = scanconversion_config["radius_end_pixels"]

    theta, r = np.meshgrid(np.linspace(initial_radius, final_radius, scanconversion_config["num_samples_along_lines"]),
                           np.linspace(radius_start_px, radius_end_px, scanconversion_config["num_lines"]))

    # Convert the polar coordinates to cartesian coordinates
    x_cart = r * np.cos(theta) + scanconversion_config["center_coordinate_pixel"][0]
    y_cart = r * np.sin(theta) + scanconversion_config["center_coordinate_pixel"][1]

    return x_cart, y_cart


def scan_interpolation_weights(scanconversion_config):
    image_size = scanconversion_config["curvilinear_image_size"]

    x_cart, y_cart = scan_conversion_inverse(scanconversion_config)
    triangulation = Delaunay(np.vstack((x_cart.flatten(), y_cart.flatten())).T)

    grid_x, grid_y = np.mgrid[0:image_size, 0:image_size]
    simplices = triangulation.find_simplex(np.vstack((grid_x.flatten(), grid_y.flatten())).T)
    vertices = triangulation.simplices[simplices]

    X = triangulation.transform[simplices, :2]
    Y = np.vstack((grid_x.flatten(), grid_y.flatten())).T - triangulation.transform[simplices, 2]
    b = np.einsum('ijk,ik->ij', X, Y)
    weights = np.c_[b, 1 - b.sum(axis=1)]

    return vertices, weights


def scan_convert(linear_data, scanconversion_config, vertices, weights):
    """
    Scan convert a linear image to a curvilinear image.

    Args:
        linear_data (np.ndarray): Linear image to be scan converted.
        scanconversion_config (dict): Dictionary with scan conversion parameters.

    Returns:
        scan_converted_image (np.ndarray): Scan converted image.
    """
    
    z = linear_data.flatten()
    zi = np.einsum('ij,ij->i', np.take(z, vertices), weights)

    image_size = scanconversion_config["curvilinear_image_size"]
    return zi.reshape(image_size, image_size)


def curvilinear_mask(scanconversion_config):
    """
    Generate a binary mask for the curvilinear image with ones inside the scan lines area and zeros outside.

    Args:
        scanconversion_config (dict): Dictionary with scan conversion parameters.

    Returns:
        mask_array (np.ndarray): Binary mask for the curvilinear image with ones inside the scan lines area and zeros outside.
    """
    angle1 = 90.0 + (scanconversion_config["angle_min_degrees"])
    angle2 = 90.0 + (scanconversion_config["angle_max_degrees"])
    center_rows_px = scanconversion_config["center_coordinate_pixel"][0]
    center_cols_px = scanconversion_config["center_coordinate_pixel"][1]
    radius1 = scanconversion_config["radius_start_pixels"]
    radius2 = scanconversion_config["radius_end_pixels"]
    image_size = scanconversion_config["curvilinear_image_size"]

    mask_array = np.zeros((image_size, image_size), dtype=np.int8)
    mask_array = cv2.ellipse(mask_array, (center_cols_px, center_rows_px), (radius2, radius2), 0.0, angle1, angle2, 1, -1)
    mask_array = cv2.circle(mask_array, (center_cols_px, center_rows_px), radius1, 0, -1)
    # Convert mask_array to uint8
    mask_array = mask_array.astype(np.uint8)

    # Repaint the borders of the mask to zero to allow erosion from all sides
    mask_array[0, :] = 0
    mask_array[:, 0] = 0
    mask_array[-1, :] = 0
    mask_array[:, -1] = 0
    
    # Erode mask by 10 percent of the image size to remove artifacts on the edges
    erosion_size = int(0.1 * image_size)
    mask_array = cv2.erode(mask_array, np.ones((erosion_size, erosion_size), np.uint8), iterations=1)
    
    return mask_array


def preprocess_input(image, image_size, scanconversion_config=None, x_cart=None, y_cart=None):
    if scanconversion_config is not None:
        # Scan convert image from curvilinear to linear
        num_samples = scanconversion_config["num_samples_along_lines"]
        num_lines = scanconversion_config["num_lines"]
        image = np.zeros((1, num_lines, num_samples))
        image[0, :, :] = map_coordinates(image[0, :, :], [x_cart, y_cart], order=1, mode='constant', cval=0.0)

    # image from slicer is (1, H, W)
    image = np.repeat(image[0, ...][:, :, np.newaxis], 3, axis=-1)  # (H, w, 3)
    image = image / 255
    image = torch.from_numpy(image)
    image = torch.permute(image, (2, 0, 1))
    image = F.interpolate(image.unsqueeze(0), size=image_size, mode="nearest")
    return image


def postprocess_masks(low_res_masks, image_size, original_size, scanconversion_config=None, vertices=None, weights=None, mask_array=None):
    # resize to model input size
    masks = F.interpolate(
        low_res_masks,
        (image_size, image_size),
        mode="bilinear",
        align_corners=False,
        )
    masks = torch.sigmoid(masks).cpu()
    
    if scanconversion_config:
        # resize to scan conversion size
        sc_h = scanconversion_config["num_lines"]
        sc_w = scanconversion_config["num_samples_along_lines"]
        masks = F.interpolate(masks, (sc_h, sc_w), mode="bilinear", align_corners=False)

        # Scan convert prediction from linear to curvilinear
        masks = scan_convert(masks[0], scanconversion_config, vertices, weights)
        masks = torch.from_numpy(masks).unsqueeze(0).unsqueeze(0)
    
    masks = F.interpolate(masks, original_size, mode="bilinear", align_corners=False)  # resize to original slicer size
    masks = masks.squeeze().numpy() * 255  # remove batch dimension = (1, H, W)
    if mask_array is not None:
        masks = masks * mask_array

    masks = masks.astype(np.uint8)[np.newaxis, ...]
    return masks


def prompt_and_decoder(args, batched_input, ddp_model, image_embeddings):
    if  batched_input["point_coords"] is not None:
        points = (batched_input["point_coords"], batched_input["point_labels"])
    else:
        points = None

    with torch.no_grad():
        sparse_embeddings, dense_embeddings = ddp_model.prompt_encoder(
            points=points,
            boxes=batched_input.get("boxes", None),
            masks=batched_input.get("mask_inputs", None),
        )

        low_res_masks, iou_predictions = ddp_model.mask_decoder(
            image_embeddings = image_embeddings,
            image_pe = ddp_model.prompt_encoder.get_dense_pe(),
            sparse_prompt_embeddings=sparse_embeddings,
            dense_prompt_embeddings=dense_embeddings,
            multimask_output=args.multimask,
        )
    
    if args.multimask:
        max_values, max_indexs = torch.max(iou_predictions, dim=1)
        max_values = max_values.unsqueeze(1)
        iou_predictions = max_values
        low_res = []
        for i, idx in enumerate(max_indexs):
            low_res.append(low_res_masks[i:i+1, idx])
        low_res_masks = torch.stack(low_res, 0)
    masks = F.interpolate(low_res_masks,(args.image_size, args.image_size), mode="bilinear", align_corners=False,)
    return masks, low_res_masks, iou_predictions


# runs the client in an infinite loop, waiting for messages from the server. Once a message is received,
# the message is processed and the inference is sent back to the server as a pyigtl ImageMessage.
def run_client(args):
    if args.log_file:
        logging.basicConfig(filename=args.log_file, filemode='w', level=logging.INFO)
    else:
        logging.basicConfig(level=logging.INFO)
    logging.info('*'*100)
    for key, value in vars(args).items():
        logging.info(key + ': ' + str(value))
    logging.info('*'*100)

    # Initialize timer and counters for profiling
    start_time = time.perf_counter()
    preprocess_counter = 0
    preprocess_total_time = 0
    inference_counter = 0
    inference_total_time = 0
    postprocess_counter = 0
    postprocess_total_time = 0
    total_counter = 0
    total_time = 0
    image_message_counter = 0
    transform_message_counter = 0

    input_client = pyigtl.OpenIGTLinkClient(host=args.host, port=args.input_port)
    output_server = pyigtl.OpenIGTLinkServer(port=args.output_port, local_server=False)

    # Load model
    model = sam_model_registry[args.model_type](args).to(args.device)
    model.eval()

    if args.scanconversion_config:
        logging.info("Loading scan conversion config...")
        with open(args.scanconversion_config, "r") as f:
            scanconversion_config = yaml.safe_load(f)
        x_cart, y_cart = scan_conversion_inverse(scanconversion_config)
        logging.info("Scan conversion config loaded")
    else:
        scanconversion_config = None
        x_cart = None
        y_cart = None
        logging.info("Scan conversion config not found")

    if x_cart is not None and y_cart is not None:
        vertices, weights = scan_interpolation_weights(scanconversion_config)
        mask_array = curvilinear_mask(scanconversion_config)
    else:
        vertices = None
        weights = None
        mask_array = None

    while True:
        # Print average inference time
        if time.perf_counter() - start_time > 1.0:
            logging.info("--------------------------------------------------")
            logging.info(f"Image messages received:   {image_message_counter}")
            logging.info(f"Transform messages received:   {transform_message_counter}")
            if preprocess_counter > 0:
                avg_preprocess_time = round((preprocess_total_time / preprocess_counter) * 1000, 1)
                logging.info(f"Average preprocess time:  {avg_preprocess_time} ms")
            if inference_counter > 0:
                avg_inference_time = round((inference_total_time / inference_counter) * 1000, 1)
                logging.info(f"Average inference time:   {avg_inference_time} ms")
            if postprocess_counter > 0:
                avg_postprocess_time = round((postprocess_total_time / postprocess_counter) * 1000, 1)
                logging.info(f"Average postprocess time: {avg_postprocess_time} ms")
            if total_counter > 0:
                avg_total_time = round((total_time / total_counter) * 1000, 1)
                logging.info(f"Average total time:       {avg_total_time} ms")
            start_time = time.perf_counter()
            preprocess_counter = 0
            preprocess_total_time = 0
            inference_counter = 0
            inference_total_time = 0
            postprocess_counter = 0
            postprocess_total_time = 0
            total_counter = 0
            total_time = 0
            image_message_counter = 0
            transform_message_counter = 0

        messages = input_client.get_latest_messages()
        for message in messages:
            if message.device_name == args.input_device_name:  # Image message
                image_message_counter += 1
                total_start_time = time.perf_counter()

                # Preprocess image
                preprocess_start_time = time.perf_counter()
                orig_img_size = message.image.shape[1:]
                image = preprocess_input(
                    message.image, args.image_size, scanconversion_config, x_cart, y_cart
                ).float().to(args.device)
                prompt_dict = {
                    "point_coords": None,
                    "point_labels": None,
                    "boxes": None
                }
                preprocess_total_time += time.perf_counter() - preprocess_start_time
                preprocess_counter += 1

                # Run inference
                inference_start_time = time.perf_counter()
                with torch.inference_mode():
                    image_embeddings = model.image_encoder(image)
                masks, low_res_masks, iou_preds = prompt_and_decoder(args, prompt_dict, model, image_embeddings)
                inference_total_time += time.perf_counter() - inference_start_time
                inference_counter += 1

                # Postprocess prediction
                postprocess_start_time = time.perf_counter()
                masks = postprocess_masks(
                    low_res_masks, args.image_size, orig_img_size, scanconversion_config, vertices, weights, mask_array
                )
                postprocess_total_time += time.perf_counter() - postprocess_start_time
                postprocess_counter += 1

                image_message = pyigtl.ImageMessage(masks, device_name=args.output_device_name)
                output_server.send_message(image_message, wait=True)

                total_time += time.perf_counter() - total_start_time
                total_counter += 1
            
            if message.message_type == "TRANSFORM" and "Image" in message.device_name:  # Image transform message
                transform_message_counter += 1
                output_tfm_name = message.device_name.replace("Image", "Pred")
                tfm_message = pyigtl.TransformMessage(message.matrix, device_name=output_tfm_name)
                output_server.send_message(tfm_message, wait=True)


if __name__ == "__main__":
    args = parse_args()
    run_client(args)
