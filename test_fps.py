# python detection.py --model_def config/cfg/yolo3d_yolov4.cfg --pretrained_path checkpoints/Model_yolo3d_yolov4.pth
# python detection.py --model_def config/cfg/yolo3d_yolov4_tiny.cfg --pretrained_path checkpoints/Model_yolo3d_yolov4_tiny.pth

# from asyncio.windows_events import NULL
import os, sys, time, datetime, argparse

from eval_mAP import evaluate_mAP
os.environ['KMP_DUPLICATE_LIB_OK']='True'
from terminaltables import AsciiTable

import numpy as np

import cv2
import torch

sys.path.append("./")
from data_process.distfile import distance_calculate_2d,  draw_line_distance, loc_object
import config.kitti_config as cnf
from data_process import kitti_utils, kitti_bev_utils
from data_process.kitti_dataloader import create_test_dataloader
from models.model_utils import create_model, make_data_parallel

from utils.evaluation_utils import load_classes, post_processing, rescale_boxes, post_processing_v2
from utils.misc import time_synchronized
from utils.mayavi_viewer import show_image_with_boxes, merge_rgb_to_bev, predictions_to_kitti_format

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    
    parser.add_argument("--model_def", type=str, default="config/cfg/yolo3d_yolov4.cfg", metavar="PATH", help="The path for cfgfile (only for darknet)")
    parser.add_argument("--pretrained_path", type=str, default="checkpoints/Model_yolo3d_yolov4.pth", metavar="PATH", help="the path of the pretrained checkpoint")
    
    # parser.add_argument("--model_def", type=str, default="config/cfg/yolo3d_yolov4_tiny.cfg", metavar="PATH", help="The path for cfgfile (only for darknet)")
    # parser.add_argument("--pretrained_path", type=str, default="checkpoints/Model_yolo3d_yolov4_tiny.pth", metavar="PATH", help="the path of the pretrained checkpoint")
    
    parser.add_argument("--saved_fn", type=str, default="yolo3d_yolov4", metavar="FN",  help="The name using for saving logs, models,...")
    parser.add_argument("-a", "--arch", type=str, default="darknet", metavar="ARCH", help="The name of the model architecture")
    parser.add_argument("--batch_size", type=int, default=1, help="size of each image batch")
    
    parser.add_argument("--use_giou_loss", action="store_true", help="If true, use GIoU loss during training. If false, use MSE loss for training")

    parser.add_argument("--no_cuda", action="store_true", help="If true, cuda is not used.")
    parser.add_argument("--gpu_idx", default=None, type=int, help="GPU index to use.")

    parser.add_argument("--num_samples", type=int, default=None, help="Take a subset of the dataset to run and debug")
    parser.add_argument("--num_workers", type=int, default=4, help="Number of threads for loading data")

    parser.add_argument("--conf_thresh", type=float, default=0.5, help="the threshold for conf")
    parser.add_argument("--nms_thresh", type=float, default=0.5, help="the threshold for conf")
    parser.add_argument("--img_size",   type=int,   default=608, help="the size of input image")
    parser.add_argument("--show_image", action="store_true", help="If true, show the image during demostration")
    
    parser.add_argument("--save_test_output", type=bool, default=True, help="If true, the output image of the testing phase will be saved")
    parser.add_argument("--output_format", type=str, default="image", metavar="PATH", help="the type of the test output (support image or video)")
    parser.add_argument("--output_video_fn", type=str, default="out_yolo3d_yolov4", metavar="PATH", help="the video filename if the output format is video")

    configs = parser.parse_args()
    
    configs.pin_memory = True

    ####################################################################
    ##############Dataset, Checkpoints, and results dir configs#########
    ####################################################################
    configs.working_dir = "./"
    configs.dataset_dir = os.path.join(configs.working_dir, "dataset", "kitti")

    if configs.save_test_output:
        configs.results_dir = os.path.join(configs.working_dir, "results", configs.saved_fn)
        if not os.path.exists(configs.results_dir):
            os.makedirs(configs.results_dir)

    configs.distributed = False  # For testing
    class_names = load_classes("dataset/classes.names")

    configs.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    # configs.device = torch.device("cpu" if configs.no_cuda else "cuda:{}".format(configs.gpu_idx))
    print(configs.device)
    
    print(configs)

    model = create_model(configs).to(configs.device)
    # model.print_network()
    
    print(configs.pretrained_path)
    
    assert os.path.isfile(configs.pretrained_path), "No file at {}".format(configs.pretrained_path)
    
    # If specified we start from checkpoint
    if configs.pretrained_path:
        if configs.pretrained_path.endswith(".pth"):
            # Data Parallel
            model = make_data_parallel(model, configs)
            model.load_state_dict(torch.load(configs.pretrained_path,map_location='cpu'))
            print("Trained pytorch weight loaded!")
        else:
            model.load_darknet_weights(configs.pretrained_path)
            # Data Parallel
            model = make_data_parallel(model, configs)
            print("Darknet weight loaded!")
    
    # model.print_network()
    # device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    # model = model.to(device)
    
    out_cap = None
    # Eval mode
    model.eval()

    test_dataloader = create_test_dataloader(configs)
    fps_c=0
    fps=0
    count = 0
    id=0
    for batch_idx, (img_paths, imgs_bev) in enumerate(test_dataloader):
        input_imgs = imgs_bev.to(configs.device).float()
        t1 = time_synchronized()
        outputs = model(input_imgs)
        t2 = time_synchronized()
        # Outputs: (batch_size x ... x 12) 12 includes: x,y,z,h,w,l,im,re,conf,cls
        with torch.no_grad():
            detections = post_processing_v2(outputs, conf_thresh=configs.conf_thresh, nms_thresh=configs.nms_thresh)

        img_detections = []  # Stores detections for each image index
        img_detections.extend(detections)

        img_bev = imgs_bev.squeeze() * 255
        img_bev = img_bev.permute(1, 2, 0).numpy().astype(np.uint8)
        img_bev = cv2.resize(img_bev, (configs.img_size, configs.img_size))
        
        
        for detections in img_detections:
            if detections is None:
                continue
            pnt=[]
            # location=[]
            # Rescale boxes to original image
            detections = rescale_boxes(detections, configs.img_size, img_bev.shape[:2])
            for x, y, z, h, w, l, im, re, *_, cls_pred in detections:
                if cls_pred != 0 :
                    yaw = torch.atan2(im, re)
                    # Draw rotated box
                    kitti_bev_utils.drawRotatedBox(img_bev, x, y, w, l, yaw,cls_pred)
                    p=(x,y)
                    # po=(x,y,z)
                    # location.append(po)
                    pnt.append(p)

        img_rgb = cv2.imread(img_paths[0])
        calib = kitti_utils.Calibration(img_paths[0].replace(".png", ".txt").replace("image_2", "calib"))
        objects_pred = predictions_to_kitti_format(img_detections, calib, img_rgb.shape, configs.img_size)
#         Locobject =loc_object(objects_pred) #location of object(x,y,z)
#         if len(Locobject) != 0:
            # dis = distance_calculate_2d(Locobject) # Function to calculate the distance of object from another object
            # dis_asli = ditanceobject_asli(id)
            # draw_line_distance(img_bev,pnt,dis)
#             img_rgb,cpoint = show_image_with_boxes(img_rgb, objects_pred, calib, False)
            # draw_line_distance(img_rgb,cpoint,dis)

        # img_bev = cv2.flip(cv2.flip(img_bev, 0), 1)

        # out_img = merge_rgb_to_bev(img_rgb, img_bev, output_width=608)

        print("\tDone testing the {}th sample, time: {:.1f}ms, speed {:.2f}FPS".format(batch_idx+1, (t2 - t1) * 1000, 1 / (t2 - t1)))
        fps=fps+(1 / (t2 - t1))
        fps_c=fps_c+1
#         id=id+1 
#         if configs.save_test_output:
#             if configs.output_format == "image":
#                 img_fn = os.path.basename(img_paths[0])[:-4]
#                 cv2.imwrite(os.path.join(configs.results_dir, "{}.jpg".format(img_fn)), img_rgb)
#             elif configs.output_format == "video":
#                 if out_cap is None:
#                     out_cap_h, out_cap_w = img_rgb.shape[:2]
#                     fourcc = cv2.VideoWriter_fourcc(*"MJPG")
#                     out_cap = cv2.VideoWriter(
#                         os.path.join(configs.results_dir, "{}.avi".format(configs.output_video_fn)),
#                         fourcc, 10, (out_cap_w, out_cap_h))

#                 out_cap.write(img_rgb)
#             else:
#                 raise TypeError

#         configs.show_image = True
        
#         if configs.show_image:
#             cv2.imshow("test-img1", img_rgb)
#             cv2.moveWindow("test-img1",100,20)
#             # cv2.imshow("test-img", out_img)
#             # print("\n[INFO] Press n to see the next sample >>> Press Esc to quit...\n")
#             if cv2.waitKey(1) & 0xFF == 27:
#                 break
            
#     # Evaulation        
#     #-------------------------------------------------------------------------------------        

#     #-------------------------------------------------------------------------------------        
#     if out_cap:
#         out_cap.release()
#     cv2.destroyAllWindows()
