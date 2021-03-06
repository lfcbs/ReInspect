"""train.py is used to generate and train the
ReInspect deep network architecture."""

import numpy as np
import json
import os
import cv2
import random
from scipy.misc import imread
import matplotlib.pyplot as plt
import caffe
import apollocaffe
from apollocaffe.models import googlenet
from apollocaffe.layers import (Power, LstmUnit, Convolution, NumpyData,
                                Transpose, Filler, SoftmaxWithLoss,
                                Softmax, Concat, Dropout, InnerProduct)

from utils import (annotation_jitter, image_to_h5,
                   annotation_to_h5, load_data_mean, Rect, stitch_rects)
from utils.annolist import AnnotationLib as al

def overlap_union(x1,y1,x2,y2,x3,y3,x4,y4):
    SI = max(0, min(x2,x4)-max(x1,x3)) * max(0, min(y2,y4)-max(y1,y3))
    SU = (x2-x1)*(y2-y1) + (x4-x3)*(y4-y3) - SI + 0.0
    return SI/SU

def get_accuracy(net, inputs, net_config):
    bbox_list, conf_list = forward(net, inputs, net_config, True)
    anno = inputs['anno']
    count = 0.0
    for r in anno:
        count += 1
    pix_per_w = net_config["img_width"]/net_config["grid_width"]
    pix_per_h = net_config["img_height"]/net_config["grid_height"]

    all_rects = [[[] for x in range(net_config["grid_width"])] for y in range(net_config["grid_height"])]
    for n in range(len(bbox_list)):
        for k in range(net_config["grid_height"] * net_config["grid_width"]):
            y = int(k / net_config["grid_width"])
            x = int(k % net_config["grid_width"])
            bbox = bbox_list[n][k]
            conf = conf_list[n][k,1].flatten()[0]
            abs_cx = pix_per_w/2 + pix_per_w*x + int(bbox[0,0,0])
            abs_cy = pix_per_h/2 + pix_per_h*y+int(bbox[1,0,0])
            w = bbox[2,0,0]
            h = bbox[3,0,0]
            all_rects[y][x].append(Rect(abs_cx,abs_cy,w,h,conf))

    acc_rects = stitch_rects(all_rects, net_config)
    count_cover = 0.0
    for rect in acc_rects:
        if rect.true_confidence < 0.9:
            continue
        else:
            x1 = rect.cx - rect.width/2.
            x2 = rect.cx + rect.width/2.
            y1 = rect.cy - rect.height/2.
            y2 = rect.cy + rect.height/2.
            for r in anno:
                o_u = overlap_union(x1,y1,x2,y2, r.x1,r.y1,r.x2,r.y2)
                if o_u >= 0.5:
                    count_cover += 1
                    break

    return (count_cover,count)

def load_idl(idlfile, data_mean, net_config, jitter=True):
    """Take the idlfile, data mean and net configuration and create a generator
    that outputs a jittered version of a random image from the annolist
    that is mean corrected."""

    annolist = al.parse(idlfile)
    annos = [x for x in annolist]
    for anno in annos:
        anno.imageName = os.path.join(
            os.path.dirname(os.path.realpath(idlfile)), anno.imageName)
    while True:
        random.shuffle(annos)
        for anno in annos:
            if jitter:
                jit_image, jit_anno = annotation_jitter(
                    anno, target_width=net_config["img_width"],
                    target_height=net_config["img_height"])
            else:
                jit_image = imread(anno.imageName)
                jit_anno = anno
            image = image_to_h5(jit_image, data_mean, image_scaling=1.0)
            boxes, box_flags = annotation_to_h5(
                jit_anno, net_config["grid_width"], net_config["grid_height"],
                net_config["region_size"], net_config["max_len"])
            yield {"imname": anno.imageName, "raw": jit_image, "image": image,
                   "boxes": boxes, "box_flags": box_flags, 'anno': jit_anno}

def generate_decapitated_googlenet(net, net_config):
    """Generates the googlenet layers until the inception_5b/output.
    The output feature map is then used to feed into the lstm layers."""

    google_layers = googlenet.googlenet_layers()
    google_layers[0].p.bottom[0] = "image"
    for layer in google_layers:
        if "loss" in layer.p.name:
            continue
        if layer.p.type in ["Convolution", "InnerProduct"]:
            for p in layer.p.param:
                p.lr_mult *= net_config["googlenet_lr_mult"]
        net.f(layer)
        if layer.p.name == "inception_5b/output":
            break

def generate_intermediate_layers(net):
    """Takes the output from the decapitated googlenet and transforms the output
    from a NxCxWxH to (NxWxH)xCx1x1 that is used as input for the lstm layers.
    N = batch size, C = channels, W = grid width, H = grid height."""

    net.f(Convolution("post_fc7_conv", bottoms=["inception_5b/output"],
                      param_lr_mults=[1., 2.], param_decay_mults=[0., 0.],
                      num_output=1024, kernel_dim=(1, 1),
                      weight_filler=Filler("gaussian", 0.005),
                      bias_filler=Filler("constant", 0.)))
    net.f(Power("lstm_fc7_conv", scale=0.01, bottoms=["post_fc7_conv"]))
    net.f(Transpose("lstm_input", bottoms=["lstm_fc7_conv"]))

def generate_ground_truth_layers(net, box_flags, boxes):
    """Generates the NumpyData layers that output the box_flags and boxes
    when not in deploy mode. box_flags = list of bitstring (e.g. [1,1,1,0,0])
    encoding the number of bounding boxes in each cell, in unary,
    boxes = a numpy array of the center_x, center_y, width and height
    for each bounding box in each cell."""

    old_shape = list(box_flags.shape)
    new_shape = [old_shape[0] * old_shape[1]] + old_shape[2:]
    net.f(NumpyData("box_flags", data=np.reshape(box_flags, new_shape)))

    old_shape = list(boxes.shape)
    new_shape = [old_shape[0] * old_shape[1]] + old_shape[2:]
    net.f(NumpyData("boxes", data=np.reshape(boxes, new_shape)))

def generate_lstm_seeds(net, num_cells):
    """Generates the lstm seeds that are used as
    input to the first lstm layer."""

    net.f(NumpyData("lstm_hidden_seed",
                    np.zeros((net.blobs["lstm_input"].shape[0], num_cells))))
    net.f(NumpyData("lstm_mem_seed",
                    np.zeros((net.blobs["lstm_input"].shape[0], num_cells))))

def get_lstm_params(step):
    """Depending on the step returns the corresponding
    hidden and memory parameters used by the lstm."""

    if step == 0:
        return ("lstm_hidden_seed", "lstm_mem_seed")
    else:
        return ("lstm_hidden%d" % (step - 1), "lstm_mem%d" % (step - 1))

def generate_lstm(net, step, lstm_params, lstm_out, dropout_ratio):
    """Takes the parameters to create the lstm, concatenates the lstm input
    with the previous hidden state, runs the lstm for the current timestep
    and then applies dropout to the output hidden state."""

    hidden_bottom = lstm_out[0]
    mem_bottom = lstm_out[1]
    num_cells = lstm_params[0]
    filler = lstm_params[1]
    net.f(Concat("concat%d" % step, bottoms=["lstm_input", hidden_bottom]))
    try:
        lstm_unit = LstmUnit("lstm%d" % step, num_cells,
                       weight_filler=filler, tie_output_forget=True,
                       param_names=["input_value", "input_gate",
                                    "forget_gate", "output_gate"],
                       bottoms=["concat%d" % step, mem_bottom],
                       tops=["lstm_hidden%d" % step, "lstm_mem%d" % step])
    except:
        # Old version of Apollocaffe sets tie_output_forget=True by default
        lstm_unit = LstmUnit("lstm%d" % step, num_cells,
                       weight_filler=filler,
                       param_names=["input_value", "input_gate",
                                    "forget_gate", "output_gate"],
                       bottoms=["concat%d" % step, mem_bottom],
                       tops=["lstm_hidden%d" % step, "lstm_mem%d" % step])
    net.f(lstm_unit)
    net.f(Dropout("dropout%d" % step, dropout_ratio,
                  bottoms=["lstm_hidden%d" % step]))

def generate_inner_products(net, step, filler):
    """Inner products are fully connected layers. They generate
    the final regressions for the confidence (ip_soft_conf),
    and the bounding boxes (ip_bbox)"""

    net.f(InnerProduct("ip_conf%d" % step, 2, bottoms=["dropout%d" % step],
                       output_4d=True,
                       weight_filler=filler))
    net.f(InnerProduct("ip_bbox_unscaled%d" % step, 4,
                       bottoms=["dropout%d" % step], output_4d=True,
                       weight_filler=filler))
    net.f(Power("ip_bbox%d" % step, scale=100,
                bottoms=["ip_bbox_unscaled%d" % step]))
    net.f(Softmax("ip_soft_conf%d" % step, bottoms=["ip_conf%d"%step]))

def generate_losses(net, net_config):
    """Generates the two losses used for ReInspect. The hungarian loss and
    the final box_loss, that represents the final softmax confidence loss"""

    net.f("""
          name: "hungarian"
          type: "HungarianLoss"
          bottom: "bbox_concat"
          bottom: "boxes"
          bottom: "box_flags"
          top: "hungarian"
          top: "box_confidences"
          top: "box_assignments"
          loss_weight: %s
          hungarian_loss_param {
            match_ratio: 0.5
            permute_matches: true
          }""" % net_config["hungarian_loss_weight"])
    net.f(SoftmaxWithLoss("box_loss",
                          bottoms=["score_concat", "box_confidences"]))

def forward(net, input_data, net_config, deploy=False):
    """Defines and creates the ReInspect network given the net, input data
    and configurations."""

    net.clear_forward()
    if deploy:
        image = np.array(input_data["image"])
    else:
        image = np.array(input_data["image"])
        box_flags = np.array(input_data["box_flags"])
        boxes = np.array(input_data["boxes"])

    net.f(NumpyData("image", data=image))
    generate_decapitated_googlenet(net, net_config)
    generate_intermediate_layers(net)
    if not deploy:
        generate_ground_truth_layers(net, box_flags, boxes)
    generate_lstm_seeds(net, net_config["lstm_num_cells"])

    filler = Filler("uniform", net_config["init_range"])
    concat_bottoms = {"score": [], "bbox": []}
    lstm_params = (net_config["lstm_num_cells"], filler)
    for step in range(net_config["max_len"]):
        lstm_out = get_lstm_params(step)
        generate_lstm(net, step, lstm_params,
                      lstm_out, net_config["dropout_ratio"])
        generate_inner_products(net, step, filler)

        concat_bottoms["score"].append("ip_conf%d" % step)
        concat_bottoms["bbox"].append("ip_bbox%d" % step)

    net.f(Concat("score_concat", bottoms=concat_bottoms["score"], concat_dim=2))
    net.f(Concat("bbox_concat", bottoms=concat_bottoms["bbox"], concat_dim=2))

    if not deploy:
        generate_losses(net, net_config)

    if deploy:
        bbox = [np.array(net.blobs["ip_bbox%d" % j].data)           # [(300,4,1,1)] * 5
                for j in range(net_config["max_len"])]
        conf = [np.array(net.blobs["ip_soft_conf%d" % j].data)      # [(300,2,1,1)] * 5
                for j in range(net_config["max_len"])]
        return (bbox, conf)
    else:
        return None

def train(config):
    """Trains the ReInspect model using SGD with momentum
    and prints out the logging information."""

    net = apollocaffe.ApolloNet()

    net_config = config["net"]
    data_config = config["data"]
    solver = config["solver"]
    logging = config["logging"]

    image_mean = load_data_mean(
        data_config["idl_mean"], net_config["img_width"],
        net_config["img_height"], image_scaling=1.0)

    input_gen_test = load_idl(data_config["test_idl"],
                                   image_mean, net_config, jitter=False)

    forward(net, input_gen_test.next(), config["net"])
    net.draw_to_file(logging["schematic_path"])

    if solver["weights"]:
        net.load(solver["weights"])
    else:
        raise Exception('weights not specified!')

    i = '1'
    output_file = open('data/new1_lstm_hidden'+i+'_mean.m','w')
    output_file.write('new1_lstm_hidden'+i+' = [')
    for _ in range(20):
        input_gen_test.next()
    for input_en in input_gen_test:
        # plt.imshow(input_en['raw'])
        # plt.show()
        forward(net, input_en, config["net"])
        lstm_hidden0 = net.blobs['lstm_hidden'+i].data       # shape: (300, 1024)
        box_flags = net.blobs['box_flags'].data         # shape: (300, 1, 5, 1)
        N = lstm_hidden0.shape[0]
        for n in range(N):
            output_file.write(str(box_flags[n,0,int(i),0])+' ')
            for c in range(lstm_hidden0.shape[1]):
                output_file.write(str(lstm_hidden0[n,c])+' ')
            output_file.write('\n')
        break
    output_file.write('];')
    output_file.close()
        

def main():
    """Sets up all the configurations for apollocaffe, and ReInspect
    and runs the trainer."""
    parser = apollocaffe.base_parser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--test_idl', required=True)
    args = parser.parse_args()
    config = json.load(open(args.config, 'r'))
    if args.weights is not None:
        config["solver"]["weights"] = args.weights
    config["solver"]["start_iter"] = args.start_iter
    config["data"]["test_idl"] = args.test_idl
    apollocaffe.set_random_seed(config["solver"]["random_seed"])
    apollocaffe.set_device(args.gpu)
    apollocaffe.set_cpp_loglevel(args.loglevel)

    train(config)

if __name__ == "__main__":
    main()

# python fc_statistic.py --gpu=0 --config=config.json --test_idl=./data/brainwash/brainwash_test.idl --weights=./data/brainwash_800000.h5