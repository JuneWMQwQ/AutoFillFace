# -*- coding: utf-8 -*-
"""
face_engine.py — 本地人脸识别引擎
- 检测：SCRFD 2.5G（ONNX，经 OpenCV DNN 推理）
- 识别：ArcFace MobileFace（ONNX，512 维 embedding，余弦相似度）
零 onnxruntime / dlib / skimage 依赖，全部走 opencv + numpy。
"""
import os
import sys

import cv2
import numpy as np

# ---------------- 相似变换（替代 skimage） ----------------

REFERENCE_LANDMARKS = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float32,
)


def _similarity_matrix(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """由 (N,2) 源点与目标点最小二乘估计相似变换 2x3 矩阵。
    模型：x' = a*x - b*y + c, y' = b*x + a*y + d
    """
    n = src.shape[0]
    A = np.zeros((2 * n, 4), dtype=np.float64)
    B = np.zeros((2 * n,), dtype=np.float64)
    for i in range(n):
        x, y = src[i]
        u, v = dst[i]
        A[2 * i] = [x, -y, 1, 0]
        B[2 * i] = u
        A[2 * i + 1] = [y, x, 0, 1]
        B[2 * i + 1] = v
    params, *_ = np.linalg.lstsq(A, B, rcond=None)
    a, b, c, d = params
    return np.array([[a, -b, c], [b, a, d]], dtype=np.float32)


def _align_face(img: np.ndarray, landmarks: np.ndarray, size: int = 112) -> np.ndarray:
    """按 5 点关键点仿射对齐人脸到 (size, size)。"""
    ratio = size / 112.0
    ref = REFERENCE_LANDMARKS * ratio
    M = _similarity_matrix(landmarks.astype(np.float64), ref.astype(np.float64))
    return cv2.warpAffine(img, M, (size, size), borderValue=0.0)


# ---------------- SCRFD 检测器（OpenCV DNN） ----------------

class SCRFD:
    def __init__(self, model_path: str, input_size=(640, 640),
                 conf_thres: float = 0.3, iou_thres: float = 0.4):
        self.input_size = input_size
        self.conf_thres = conf_thres
        self.iou_thres = iou_thres
        self.fmc = 3
        self.strides = [8, 16, 32]
        self.num_anchors = 2
        self.mean = 127.5
        self.std = 128.0
        self._center_cache: dict = {}
        self.net = cv2.dnn.readNetFromONNX(model_path)
        # 建立 stride -> {scores/bbox/kps} 输出映射
        self._output_map = self._map_outputs()

    def _map_outputs(self) -> dict:
        """按输出张量形状推断各 stride 的 scores/bbox/kps。"""
        un = self.net.getUnconnectedOutLayers().flatten()
        names = [self.net.getLayerNames()[i - 1] for i in un]
        probe = np.zeros((self.input_size[1], self.input_size[0], 3), dtype=np.uint8)
        self.net.setInput(cv2.dnn.blobFromImage(
            probe, 1.0 / self.std, self.input_size,
            (self.mean,) * 3, swapRB=True))
        outs = self.net.forward(names)
        mapping: dict = {}
        for o in outs:
            n = int(o.shape[0])
            side = int(round((n / self.num_anchors) ** 0.5))
            stride = self.input_size[0] // side
            cols = int(o.shape[1])
            kind = {1: "scores", 4: "bbox", 10: "kps"}[cols]
            mapping.setdefault(stride, {})[kind] = o
        return mapping

    def _forward(self, det_image: np.ndarray) -> dict:
        """前向推理，返回 {stride: {scores, bbox, kps}}（未乘 stride）。"""
        blob = cv2.dnn.blobFromImage(
            det_image, 1.0 / self.std, self.input_size,
            (self.mean, self.mean, self.mean), swapRB=True)
        self.net.setInput(blob)
        un = self.net.getUnconnectedOutLayers().flatten()
        names = [self.net.getLayerNames()[i - 1] for i in un]
        outs = self.net.forward(names)
        result: dict = {}
        idx = 0
        for stride in sorted(self._output_map.keys()):
            result[stride] = {
                "scores": outs[idx],
                "bbox": outs[idx + 1],
                "kps": outs[idx + 2],
            }
            idx += 3
        return result

    @staticmethod
    def _distance2bbox(points, distance):
        x1 = points[:, 0] - distance[:, 0]
        y1 = points[:, 1] - distance[:, 1]
        x2 = points[:, 0] + distance[:, 2]
        y2 = points[:, 1] + distance[:, 3]
        return np.stack([x1, y1, x2, y2], axis=-1)

    @staticmethod
    def _distance2kps(points, distance):
        preds = []
        for i in range(0, distance.shape[1], 2):
            px = points[:, i % 2] + distance[:, i]
            py = points[:, i % 2 + 1] + distance[:, i + 1]
            preds.extend([px, py])
        return np.stack(preds, axis=-1)

    @staticmethod
    def _nms(dets: np.ndarray, iou_thres: float):
        x1, y1, x2, y2, scores = dets[:, 0], dets[:, 1], dets[:, 2], dets[:, 3], dets[:, 4]
        areas = (x2 - x1 + 1) * (y2 - y1 + 1)
        order = scores.argsort()[::-1]
        keep = []
        while order.size > 0:
            i = order[0]
            keep.append(i)
            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])
            w = np.maximum(0.0, xx2 - xx1 + 1)
            h = np.maximum(0.0, yy2 - yy1 + 1)
            inter = w * h
            ovr = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
            order = order[np.where(ovr <= iou_thres)[0] + 1]
        return keep

    def detect(self, image_bgr: np.ndarray):
        """返回 (boxes(N,5), kpss(N,5,2))，坐标在原图像素。"""
        h, w = self.input_size
        im_ratio = image_bgr.shape[0] / image_bgr.shape[1]
        model_ratio = h / w
        if im_ratio > model_ratio:
            new_h, new_w = h, int(h / im_ratio)
        else:
            new_w, new_h = w, int(w * im_ratio)
        det_scale = new_h / image_bgr.shape[0]
        resized = cv2.resize(image_bgr, (new_w, new_h))
        det_img = np.zeros((h, w, 3), dtype=np.uint8)
        det_img[:new_h, :new_w, :] = resized

        outs = self._forward(det_img)
        scores_list, bboxes_list, kpss_list = [], [], []
        for stride in self.strides:
            if stride not in outs:
                continue
            o = outs[stride]
            scores = o["scores"]
            bboxes = self._distance2bbox(
                self._anchor_centers(h // stride, w // stride, stride),
                o["bbox"] * stride)
            kps = self._distance2kps(
                self._anchor_centers(h // stride, w // stride, stride),
                o["kps"] * stride).reshape((-1, 5, 2))
            pos = np.where(scores >= self.conf_thres)[0]
            if pos.size == 0:
                continue
            scores_list.append(scores[pos])
            bboxes_list.append(bboxes[pos])
            kpss_list.append(kps[pos])
        if not scores_list:
            return np.zeros((0, 5), dtype=np.float32), np.zeros((0, 5, 2), dtype=np.float32)

        scores = np.vstack(scores_list).ravel()
        boxes = np.vstack(bboxes_list) / det_scale
        kps = np.vstack(kpss_list) / det_scale
        order = scores.argsort()[::-1]
        pre_det = np.hstack((boxes, scores.reshape(-1, 1))).astype(np.float32)
        pre_det = pre_det[order]
        kps = kps[order]
        keep = self._nms(pre_det, self.iou_thres)
        return pre_det[keep], kps[keep]

    def _anchor_centers(self, h: int, w: int, stride: int) -> np.ndarray:
        key = (h, w, stride)
        if key not in self._center_cache:
            centers = np.stack(np.mgrid[:h, :w][::-1], axis=-1).astype(np.float32)
            centers = (centers * stride).reshape((-1, 2))
            centers = np.stack([centers] * self.num_anchors, axis=1).reshape((-1, 2))
            self._center_cache[key] = centers
        return self._center_cache[key]


# ---------------- ArcFace 识别（OpenCV DNN） ----------------

class ArcFace:
    def __init__(self, model_path: str):
        self.net = cv2.dnn.readNetFromONNX(model_path)
        self.size = 112

    def get_embedding(self, aligned_bgr: np.ndarray) -> np.ndarray:
        """输入已对齐的 BGR 人脸图，输出 512 维 L2 归一化 embedding。"""
        resized = cv2.resize(aligned_bgr, (self.size, self.size))
        blob = cv2.dnn.blobFromImage(
            resized, 1.0 / 127.5, (self.size, self.size),
            (127.5, 127.5, 127.5), swapRB=True)
        self.net.setInput(blob)
        emb = self.net.forward().flatten()
        norm = np.linalg.norm(emb)
        return emb / norm if norm > 0 else emb


# ---------------- 人脸引擎（注册 + 验证） ----------------

def resource_path(rel: str) -> str:
    """打包后从 _MEIPASS 取资源；源码运行时相对项目根目录。"""
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


class FaceEngine:
    def __init__(self, threshold: float = 0.55,
                 det_path: str | None = None, rec_path: str | None = None):
        det_path = det_path or resource_path(os.path.join("assets", "models", "det_2.5g.onnx"))
        rec_path = rec_path or resource_path(os.path.join("assets", "models", "w600k_mbf.onnx"))
        self.detector = SCRFD(det_path)
        self.recognizer = ArcFace(rec_path)
        self.threshold = threshold
        self.templates: list[np.ndarray] = []
        if getattr(sys, "frozen", False):
            self.template_path = os.path.join(os.path.dirname(sys.executable), "face.bin")
        else:
            self.template_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "face.bin")
        self.load_templates()

    def set_threshold(self, t: float) -> None:
        self.threshold = float(t)

    # ---------- 模板持久化（DPAPI 加密） ----------
    def save_templates(self) -> None:
        if not self.templates:
            return
        arr = np.stack(self.templates)
        try:
            from vault import _dpapi_encrypt
            enc = _dpapi_encrypt(arr.tobytes())
            with open(self.template_path, "wb") as f:
                f.write(enc)
        except Exception:
            pass  # 加密失败不落明文

    def load_templates(self) -> None:
        self.templates = []
        if not os.path.exists(self.template_path):
            return
        try:
            from vault import _dpapi_decrypt
            raw = _dpapi_decrypt(open(self.template_path, "rb").read())
            arr = np.frombuffer(raw, dtype=np.float32)
            if arr.size % 512 == 0 and arr.size > 0:
                self.templates = [arr[i:i + 512].copy()
                                  for i in range(0, arr.size, 512)]
        except Exception:
            self.templates = []

    def clear_templates(self) -> None:
        self.templates = []
        try:
            if os.path.exists(self.template_path):
                os.remove(self.template_path)
        except Exception:
            pass

    # ---------- 单帧提取 ----------
    def extract_largest(self, bgr: np.ndarray):
        """检测最大人脸并返回 (bbox, embedding)；无人脸返回 None。"""
        boxes, kpss = self.detector.detect(bgr)
        if boxes.shape[0] == 0:
            return None
        areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        best = int(np.argmax(areas))
        kp = kpss[best]
        aligned = _align_face(bgr, kp)
        emb = self.recognizer.get_embedding(aligned)
        return boxes[best], emb

    # ---------- 注册 ----------
    def register_from_frames(self, frames: list[np.ndarray], max_embs: int = 8,
                             dedup_thresh: float = 0.82) -> tuple[list[np.ndarray], int]:
        """从多帧采集人脸 embedding。返回 (模板列表, 成功帧数)。"""
        templates: list[np.ndarray] = []
        for frame in frames:
            try:
                result = self.extract_largest(frame)
            except Exception:
                continue
            if result is None:
                continue
            _, emb = result
            if templates:
                sims = [float(np.dot(t, emb)) for t in templates]
                if max(sims) > dedup_thresh:
                    continue
            templates.append(emb)
            if len(templates) >= max_embs:
                break
        return templates, len(templates)

    # ---------- 验证 ----------
    def verify_from_frames(self, frames: list[np.ndarray],
                           templates: list[np.ndarray]) -> tuple[bool, float, int]:
        """多帧验证。返回 (是否通过, 最高相似度, 达标帧数)。"""
        if not templates:
            return False, 0.0, 0
        passed = 0
        best_sim = 0.0
        for frame in frames:
            try:
                result = self.extract_largest(frame)
            except Exception:
                continue
            if result is None:
                continue
            _, emb = result
            sim = max(float(np.dot(t, emb)) for t in templates)
            best_sim = max(best_sim, sim)
            if sim >= self.threshold:
                passed += 1
        ok = passed >= max(1, int(len(frames) * 0.5))
        return ok, best_sim, passed
