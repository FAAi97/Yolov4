import numpy as np
import mayavi.mlab as mlab
import cv2
from scipy.spatial import distance as dist
import sys
sys.path.append('../')

import math
from data_process import kitti_utils, kitti_bev_utils, kitti_aug_utils
import config.kitti_config as cnf

# ===========================
# ----------- 3d ------------
# ===========================  
def Center_Point(img,objects, calib):
    img2 = np.copy(img) # for 3d bbox
    centerpoint=[]
    for obj in objects:
        if obj.type == 'DontCare': continue 
         # cv2.rectangle(img2, (int(obj.xmin),int(obj.ymin)),
         #    (int(obj.xmax),int(obj.ymax)), (0,255,0), 2)
        box3d_pts_2d, box3d_pts_3d= kitti_utils.compute_box_3d(obj, calib.P)
        if box3d_pts_2d is not None:
            p = [(box3d_pts_2d[1, 0] + box3d_pts_2d[7,0])/2 , (box3d_pts_2d[1,1] +box3d_pts_2d[7,1])/2] 
            centerpoint.append(p) 
    return centerpoint


# ===========================
# ----------- 3d ------------
# ===========================    
def distance_calculate_3d(centerpoint,objects):
    Dis = []
    length = len(objects)
    for j in range(length): 
        #q = j+1
        for q in range(j+1,length):
            if q >= len(centerpoint): break
            D = dist.euclidean(centerpoint[q],centerpoint[j])
            #print(j,q,D)
            print("class "+str(j)+" class "+str(q)+" distance  is: "+ str(D)) 
            Dis.append(D)
    return Dis

# ===========================
# ----------- 2d ------------
# ===========================
def distance_calculate_2d(point):
    # Function to calculate the distance of object from another object
    Dis = []
    length = len(point)
    for j in range(length): 
        #q = j+1
        for q in range(j+1,length):
            if q >= len(point): break
            D = dist.euclidean(point[q],point[j])
            #print(j,q,D)
            print("object "+str(j)+" object "+str(q)+" distance  is: "+ str(D)) 
            Dis.append((D))
    return Dis



# ===========================
# ----------- 3d ------------
# ===========================
def loc_object(objects):
    centerobject=[]
    for obj in objects: 
         if obj.type == 'DontCare': continue 
         elif obj.type == 'Car': continue 
         [xloc,yloc,zloc,_]=kitti_utils.Object3d.loc_object(obj) #calculate location meter
         centerobject.append([xloc,yloc,zloc])
    return centerobject



def draw_line_distance(image,qs,dis,thickness=2):
    length = len(qs)
    # qs = [int(q) for q in qs]
    # qs = qs.astype(np.int32)
    q=0
    for i in range(length):
         for j in range(i+1,length):
            if dis[q]<1.5:
                color=[0,0,255]
                cv2.line(image, (int(qs[i][0]), int(qs[i][1])), (int(qs[j][0]), int(qs[j][1])), color, thickness)
            q=q+1