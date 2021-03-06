# TODO
# Add hard negatives so that exemplars are more descriminative, right now they are too "soft"
# Add full positive images to validation set so that the exemplars aren't so easily hacked
import picarus
import hadoopy
import imfeat
import numpy as np
import cv2
import hashlib
import glob
import time
import logging
import feature
import sklearn
import random
import cPickle as pickle
import os
import shutil
import hadoopy_helper
import gc
logging.basicConfig(level=logging.INFO)
hadoopy_helper.prefreeze('*.py')

LAUNCH_HOLDER = None
def toggle_launch():
    global LAUNCH_HOLDER
    if LAUNCH_HOLDER is None:
        LAUNCH_HOLDER = hadoopy.launch, hadoopy.launch_frozen, hadoopy.launch_local
        hadoopy.launch = hadoopy.launch_frozen = hadoopy.launch_local = lambda *x, **y: {}
    else:
        hadoopy.launch, hadoopy.launch_frozen, hadoopy.launch_local = LAUNCH_HOLDER
        LAUNCH_HOLDER = None


def cleanup_image(image):
    if image is None:
        raise ValueError('Bad image')
    image = remove_tiling(image)
    # if the image is > 1024, make the largest side 1024
    if np.min(image.shape[:2]) < 2 * feature.PATCH_SIZE:
        print('Skipping [%s]' % (image.shape[:2],))
        raise ValueError('Image too small')
    max_side = 512
    if np.max(image.shape[:2]) > max_side:
        height, width = (max_side * np.array(image.shape[:2]) / np.max(image.shape[:2])).astype(np.int)
        print(image.shape)
        image = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
        print('Resizing to (%d, %d)' % (height, width))
    return image


def load_data_iter(local_inputs):
    # Push labeled samples to HDFS
    unique_ids = set()
    for fn in local_inputs:
        try:
            image_data = imfeat.image_tostring(cleanup_image(cv2.imread(fn)), '.jpg')
        except (ValueError, IndexError):
            continue
        image_id = hashlib.md5(image_data).hexdigest()
        if image_id not in unique_ids:
            unique_ids.add(image_id)
            yield image_id, image_data


def setup_data(local_inputs, hdfs_input, images_per_file=2):
    cnt = 0
    out = []
    for x in load_data_iter(local_inputs):
        out.append(x)
        if len(out) > images_per_file:
            hadoopy.writetb(hdfs_input + '/%d' % cnt, out)
            cnt += 1
            out = []
    if out:
        hadoopy.writetb(hdfs_input + '/%d' % cnt, out)


def remove_tiling(frame):
    nums = []
    try:
        # Bottom
        num = 0
        while np.all(frame[-(num + 2), :, :] == frame[-1, :, :]):
            num += 1
        if num >= 1:
            frame = frame[:-(num + 1), :, :]
        nums.append(num)
        # Top
        num = 0
        while np.all(frame[num + 1, :, :] == frame[0, :, :]):
            num += 1
        if num >= 1:
            frame = frame[num:, :, :]
        nums.append(num)
        # Right
        num = 0
        while np.all(frame[:, -(num + 2), :] == frame[:, -1, :]):
            num += 1
        if num >= 1:
            frame = frame[:, :-(num + 1), :]
        nums.append(num)
        # Left
        num = 0
        while np.all(frame[:, num + 1, :] == frame[:, 0, :]):
            num += 1
        if num >= 1:
            frame = frame[:, num:, :]
    except IndexError:
        print(frame.shape)
        print(num)
        raise
    nums.append(num)
    if np.sum(nums):
        print(nums)
    return frame


def initial_train(hdfs_input, hdfs_output):
    hadoopy.launch_frozen(hdfs_input + '0-tr', hdfs_output + 'neg', 'compute_exemplar_features.py', remove_output=True)
    hadoopy.launch_frozen(hdfs_input + '1-tr', hdfs_output + 'pos', 'compute_exemplar_features.py', remove_output=True)
    # Compute desired probability
    num_val = 5000
    num_neg_train = 5000
    toggle_launch()
    if 0:
        neg_samples = list(hadoopy_helper.jobs.random_sample(hdfs_output + 'neg', num_val + num_neg_train))
        neg_samples = [x[1] for x in neg_samples]
        with open('neg_feats.pkl', 'w') as fp:
            pickle.dump(np.array(neg_samples[num_val:]), fp, -1)
        with open('neg_val_feats.pkl', 'w') as fp:
            pickle.dump(np.array(neg_samples[:num_val]), fp, -1)
        del neg_samples
        gc.collect()
        pos_samples = list(hadoopy_helper.jobs.random_sample(hdfs_output + 'pos', num_val / 2))  # Twice as many neg as positive
        pos_samples = [x[1] for x in pos_samples]
        with open('pos_val_feats.pkl', 'w') as fp:
            pickle.dump(np.array(pos_samples), fp, -1)
        del pos_samples
    gc.collect()
    cmdenvs = {'NEG_FEATS': 'neg_feats.pkl',
               'POS_VAL_FEATS': 'pos_val_feats.pkl',
               'NEG_VAL_FEATS': 'neg_val_feats.pkl'}
    files = cmdenvs.values()
    cmdenvs['SAMPLE_SIZE'] = 1000
    hadoopy.launch_frozen(hdfs_output + 'pos', hdfs_output + 'exemplars-0', 'uniform_selection.py',
                          cmdenvs=cmdenvs, remove_output=True, files=files)
    exemplar_out = sorted(hadoopy.readtb(hdfs_output + 'exemplars-0'), key=lambda x: x[0])
    with open('exemplars.pkl', 'w') as fp:
        pickle.dump(exemplar_out, fp, -1)


def hard_train(hdfs_input, hdfs_output):
    hadoopy.launch_frozen(hdfs_input + '0-tr', hdfs_output + 'hard_neg', 'hard_predictions.py', cmdenvs=['EXEMPLARS=exemplars.pkl',
                                                                                                         'MAX_HARD=100',
                                                                                                         'OUTPUT_FORMAT=score_image_box'],
                          num_reducers=10, files=['exemplars.pkl'], remove_output=True)

    def _inner():
        with open('image_box_fns.pkl', 'w') as fp:
            image_box_fns = {}
            for (image_id, box, score), negs in hadoopy.readtb(hdfs_output + 'hard_neg'):
                for score2, image_id2, box2 in negs:
                    image_box_fns.setdefault(image_id2, []).append((box2, [image_id, box, score]))
            pickle.dump(image_box_fns, fp, -1)
        del image_box_fns
        gc.collect()
    _inner()
    hadoopy.launch_frozen(hdfs_input + '0-tr', hdfs_output + 'hard_neg_clip', 'clip_boxes.py', files=['image_box_fns.pkl'], remove_output=True, cmdenvs=['TYPE=feature'])
    hadoopy.launch_frozen([hdfs_output + 'pos_sample',
                           hdfs_output + 'hard_neg_clip'], hdfs_output + 'exemplars-1', 'train_exemplars_hard.py',
                          cmdenvs=['NEG_FEATS=neg_feats.pkl', 'MAX_HARD=200'], files=['neg_feats.pkl'],
                          remove_output=True, num_reducers=10)
    exemplar_out = sorted(hadoopy.readtb(hdfs_output + 'exemplars-1'), key=lambda x: x[0])
    with open('exemplars.pkl', 'w') as fp:
        pickle.dump(exemplar_out, fp, -1)


def calibrate(hdfs_input, hdfs_output):
    # Predict on pos/neg sets
    hadoopy.launch_frozen(hdfs_input + '1-v', hdfs_output + 'val_pos', 'image_predict.py', cmdenvs=['EXEMPLARS=exemplars.pkl', 'CELL_SKIP=16'], remove_output=True, num_reducers=10, files=['exemplars.pkl'])
    hadoopy.launch_frozen(hdfs_input + '0-v', hdfs_output + 'val_neg', 'image_predict.py', cmdenvs=['EXEMPLARS=exemplars.pkl', 'CELL_SKIP=1'], remove_output=True, num_reducers=10, files=['exemplars.pkl'])
    # Calibrate threshold using pos/neg validation set #1
    hadoopy.launch_frozen([hdfs_output + 'val_neg', hdfs_output + 'val_pos', hdfs_output + 'exemplars-1'], hdfs_output + 'exemplars-2', 'calibrate_thresholds.py', num_reducers=50, remove_output=True)
    exemplar_out = sorted(hadoopy.readtb(hdfs_output + 'exemplars-2'), key=lambda x: x[0])
    with open('exemplars.pkl', 'w') as fp:
        pickle.dump(exemplar_out, fp, -1)


def output_exemplars(hdfs_input, hdfs_output, num=2, output_type='box', output_path='exemplars'):
    with open('image_box_fns.pkl', 'w') as fp:
        image_box_fns = {}
        for (image_id, box, score), _ in hadoopy.readtb(hdfs_output + 'exemplars-%d' % num):
            image_box_fns.setdefault(image_id, []).append((box, 'exemplar-%.5d-%s-%s.png' % (score, image_id, box)))
        pickle.dump(image_box_fns, fp, -1)
    hadoopy.launch_frozen(hdfs_input + '1-tr', hdfs_output + 'exemplars-%d-clip' % num, 'clip_boxes.py', files=['image_box_fns.pkl'], remove_output=True, cmdenvs=['TYPE=%s' % output_type])
    try:
        shutil.rmtree(output_path)
    except OSError:
        pass
    os.makedirs(output_path)
    for x, y in hadoopy.readtb(hdfs_output + 'exemplars-%d-clip' % num):
        open(output_path + '/%s' % (x,), 'w').write(y)


def cluster(hdfs_input, hdfs_output):
    hadoopy.launch_frozen(hdfs_input + '1-v', hdfs_output + 'val_pred_pos', 'predict_spatial_pyramid_fine.py', cmdenvs=['EXEMPLARS=exemplars.pkl'], remove_output=True, files=['exemplars.pkl'], num_reducers=1)

    #with open('labels.pkl', 'w') as fp:
    #    pickle.dump(list(hadoopy_helper.jobs.unique_keys(hdfs_output + 'val_pred_pos')), fp, -1)
    #picarus.classify.run_compute_kernels(hdfs_output + 'val_pred_pos', hdfs_output + 'val_pred_pos_kern', 'labels.pkl', 'labels.pkl', remove_output=True, num_reducers=20, jobconfs=['mapred.child.java.opts=-Xmx256M'], cols_per_chunk=500)
    #picarus.classify.run_assemble_kernels(hdfs_output + 'val_pred_pos_kern', hdfs_output + 'val_pred_pos_kern2', remove_output=True)


def workflow(hdfs_input, hdfs_output):
    toggle_launch()
    initial_train(hdfs_input, hdfs_output)
    output_exemplars(hdfs_input, hdfs_output, 0)
    hard_train(hdfs_input, hdfs_output)
    calibrate(hdfs_input, hdfs_output)
    output_exemplars(hdfs_input, hdfs_output)
    cluster(hdfs_input, hdfs_output)


    # TODO Use the calibrated offset and current offset to determine what threshold to predict on the previous hard prediction run
    # TODO Check that the libsvm prediction is thresholded at 0
    
    # Predict on positive validation set #2, produce sparse spatial pyramid binned detections and reduce (collect rows)
    # Compute similarity matrix for sparse detections
    # Compute hierarchical clustering of similarity matrix
    pass


def exemplar_boxes(hdfs_input, hdfs_output):
    exemplar_name = 'ad813d130f4803e948124823a67cdd7b-[0.0, 0.16326530612244897, 0.3448275862068966, 0.5714285714285714]'
    st = time.time()
    exemplar_out = hadoopy.abspath(hdfs_output + 'exemplar_boxes/%s' % st) + '/'
    for kv in hadoopy.readtb(hdfs_output + 'exemplars-2'):
        (image_id, box, score), _ = kv
        if exemplar_name == '%s-%s' % (image_id, box):
            print('Found it')
            with open('exemplars-patch.pkl', 'w') as fp:
                pickle.dump([kv], fp, -1)
    hadoopy.launch_frozen(hdfs_input + '1-v', exemplar_out + 'val_pos', 'hard_predictions.py', cmdenvs=['EXEMPLARS=exemplars-patch.pkl', 'MAX_HARD=100', 'OUTPUT_FORMAT=score_image_box'], files=['exemplars-patch.pkl'],
                          num_reducers=10)
    hadoopy.launch_frozen(hdfs_input + '0-v', exemplar_out + 'val_neg', 'hard_predictions.py', cmdenvs=['EXEMPLARS=exemplars-patch.pkl', 'MAX_HARD=100', 'OUTPUT_FORMAT=score_image_box'], files=['exemplars-patch.pkl'],
                          num_reducers=10)
    with open('image_box_fns.pkl', 'w') as fp:
        image_box_fns = {}
        pos_boxes = [(score, image_id, box, 1) for score, image_id, box in sorted(hadoopy.readtb(exemplar_out + 'val_pos').next()[1])]
        neg_boxes = [(score, image_id, box, 0) for score, image_id, box in sorted(hadoopy.readtb(exemplar_out + 'val_neg').next()[1])]
        for num, (score, image_id, box, pol) in enumerate(sorted(pos_boxes + neg_boxes, reverse=True)):
            image_box_fns.setdefault(image_id, []).append((box, 'exemplar-%.5d-%d-%f.png' % (num, pol, score)))
        pickle.dump(image_box_fns, fp, -1)
    hadoopy.launch_frozen([hdfs_input + '1-v', hdfs_input + '0-v'], exemplar_out + 'boxes_cropped', 'clip_boxes.py', files=['image_box_fns.pkl'], remove_output=True, cmdenvs={'TYPE': 'image'})
    out_dir = 'exemplars_similar_cropped/'
    try:
        shutil.rmtree('exemplars_similar_cropped')
    except OSError:
        pass
    print('Outputting cropped')
    os.makedirs(out_dir)
    print(exemplar_out + 'boxes_cropped')
    for x, y in hadoopy.readtb(exemplar_out + 'boxes_cropped'):
        open(out_dir + x, 'w').write(y)

    hadoopy.launch_frozen([hdfs_input + '1-v', hdfs_input + '0-v'], exemplar_out + 'boxes', 'clip_boxes.py', files=['image_box_fns.pkl'], remove_output=True, cmdenvs={'TYPE': 'box'})
    out_dir = 'exemplars_similar/'
    try:
        shutil.rmtree('exemplars_similar')
    except OSError:
        pass
    print('Outputting boxes')
    os.makedirs(out_dir)
    for x, y in hadoopy.readtb(exemplar_out + 'boxes'):
        open(out_dir + x, 'w').write(y)

def main():
    #toggle_launch()
    local_input, hdfs_input = '/home/brandyn/playground/sun_labelme/person/', 'exemplarbank/data/sun_labelme_person/'
    #local_input, hdfs_input = '/home/brandyn/playground/aladdin_data_cropped/person/', 'exemplarbank/data/aladdin_person/'
    neg_local_inputs = glob.glob('%s/%d/*' % (local_input, 0))
    pos_local_inputs = glob.glob('%s/%d/*' % (local_input, 1))
    random.shuffle(neg_local_inputs)
    random.shuffle(pos_local_inputs)
    print(len(neg_local_inputs))
    print(len(pos_local_inputs))
    train_ind = int(.5 * len(neg_local_inputs))
    #setup_data(neg_local_inputs[:train_ind], hdfs_input + '0-tr')
    #setup_data(neg_local_inputs[train_ind:], hdfs_input + '0-v')
    #setup_data(pos_local_inputs[:train_ind], hdfs_input + '1-tr')
    #setup_data(pos_local_inputs[train_ind:], hdfs_input + '1-v')
    hdfs_output = 'exemplarbank/output/%s/' % '1341790878.92'  # time.time()
    #exemplar_boxes(hdfs_input, hdfs_output)
    workflow(hdfs_input, hdfs_output)
    #output_exemplars(hdfs_input, hdfs_output, 2, 'image', 'exemplars_cropped')


if __name__ == '__main__':
    main()
