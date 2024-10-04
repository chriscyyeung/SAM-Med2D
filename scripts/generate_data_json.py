import os
import json
import argparse


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=str, help="input folder")
    parser.add_argument("--output", type=str, help="output folder")
    parser.add_argument(
        "--type", 
        type=str, 
        default="train", 
        choices=["train", "test"],
        help="train or test set"
    )
    parser.add_argument(
        "--img-dir-name", 
        type=str, 
        default="imgs", 
        help="name of image folders"
    )
    parser.add_argument(
        "--gt-dir-name", 
        type=str, 
        default="gts", 
        help="name of gt folders"
    )
    return parser.parse_args()


def main(args):
    imgs = []
    gts = []
    for root, dirs, files in os.walk(args.input):
        if args.img_dir_name in root.split(os.sep) and not dirs:
            imgs += [os.path.join(root, fn) for fn in files]
        if args.gt_dir_name in root.split(os.sep) and not dirs:
            gts += [os.path.join(root, fn) for fn in files]
    
    if args.type == "train":
        data_dict = {key: [value] for key, value in zip(imgs, gts)}
        output_fn = "image2label_train.json"
    else:
        data_dict = dict(zip(gts, imgs))
        output_fn = "label2image_test.json"

    # save to json
    if args.output:
        output_dir = args.output
    else:
        output_dir = args.input
    output_path = os.path.join(output_dir, output_fn)
    with open(output_path, "w") as f:
        json.dump(data_dict, f)
    print(f"Saved data paths to {output_path}.")


if __name__ == "__main__":
    args = parse_args()
    main(args)
