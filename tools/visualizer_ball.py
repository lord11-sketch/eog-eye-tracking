#!/usr/bin/env python3
"""EOG ball visualiser (pygame).

A ball follows the deflection of the EOG signal from its calibrated baseline.
Demonstration / electrode-placement aid only: it uses a plain rolling average
of the raw signal, not any of the methods under test, and records nothing.

    python tools/visualizer_ball.py                       # horizontal + vertical
    python tools/visualizer_ball.py --axis vertical       # up/down only, from the first live input
    python tools/visualizer_ball.py --port COM5 --duration 0

Keys:  ESC / Q quit    R re-calibrate    I flip direction

This merges the original ``visualizer_ball.py`` and ``visualizer_ball_vertical.py``
(``--axis vertical`` reproduces the latter: one EXG Pill on either input drives
the vertical axis).
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import threading
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eog import io as eio                    # noqa: E402
from eog.detector import Calibrator          # noqa: E402

# ----- configuration (amplitudes are raw ADC counts) -----
DEAD_ZONE = 300          # counts around the baseline that produce no movement
MAX_DEFLECTION = 2000    # counts that map to the edge of the play area
SMOOTHING_S = 0.2        # rolling-average window, seconds of samples
BALL_RADIUS = 40
CALIB_SEC = 3.0
CALIB_SAMPLES = 30
MARGIN = 80

BG = (15, 15, 25)
BALL = (50, 200, 255)
GLOW = (20, 80, 140)
FAINT = (60, 60, 80)
TEXT = (200, 200, 220)
NEG = (255, 80, 80)
POS = (80, 255, 120)
BAR_BG = (40, 40, 55)


class SerialReader(threading.Thread):
    """Reads validated samples in the background and queues every one of them
    (the original kept only the latest value, so calibration and smoothing saw
    duplicated or skipped samples depending on the frame rate)."""

    def __init__(self, port):
        super().__init__(daemon=True)
        self.stream = eio.LineStream(port, no_data_timeout_s=5.0)
        self._queue = deque(maxlen=5000)
        self._lock = threading.Lock()
        self.stopped = False

    def run(self):
        for sample in self.stream:
            with self._lock:
                self._queue.append(sample)
        self.stopped = True            # port error or data timeout (see stream.stats.stop_reason)

    def drain(self):
        with self._lock:
            items = list(self._queue)
            self._queue.clear()
        return items


class RollingMean:
    def __init__(self, window):
        self.buf = deque(maxlen=max(1, window))

    def feed(self, value):
        self.buf.append(value)
        return sum(self.buf) / len(self.buf)

    def reset(self):
        self.buf.clear()


def to_norm(value):
    """Deflection in counts -> [-1, 1] with a dead zone."""
    if abs(value) < DEAD_ZONE:
        return 0.0
    sign = 1 if value > 0 else -1
    return sign * min(1.0, (abs(value) - DEAD_ZONE) / (MAX_DEFLECTION - DEAD_ZONE))


def pick(sample, axis_mode):
    """(horizontal value, vertical value) for the chosen mode."""
    if axis_mode == "vertical":
        return None, (sample.h if sample.h is not None else sample.v)
    if axis_mode == "horizontal":
        return sample.h, None
    return sample.h, sample.v


def calibrate(pygame, reader, screen, fonts, axis_mode):
    """Full-screen calibration; returns (baseline_h, baseline_v), None where unused."""
    cals = [Calibrator(CALIB_SEC, CALIB_SAMPLES), Calibrator(CALIB_SEC, CALIB_SAMPLES)]
    clock = pygame.time.Clock()
    t0 = time.monotonic()
    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT or (
                    event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE, pygame.K_q)):
                return None
        if reader.stopped:
            return None
        now = time.monotonic() - t0
        for sample in reader.drain():
            for cal, value in zip(cals, pick(sample, axis_mode)):
                if value is not None:
                    cal.feed(value, now)
        active = [c for c in cals if c.values]
        progress = min(1.0, now / CALIB_SEC)
        w, h = screen.get_size()
        screen.fill(BG)
        title = fonts["big"].render("CALIBRATING", True, TEXT)
        screen.blit(title, title.get_rect(center=(w // 2, h // 2 - 80)))
        hint = fonts["normal"].render("Look straight ahead and stay still", True, (150, 150, 170))
        screen.blit(hint, hint.get_rect(center=(w // 2, h // 2 - 40)))
        bw, bh = 400, 20
        bx, by = w // 2 - bw // 2, h // 2 + 10
        pygame.draw.rect(screen, BAR_BG, (bx, by, bw, bh), border_radius=8)
        pygame.draw.rect(screen, BALL, (bx, by, int(bw * progress), bh), border_radius=8)
        pygame.display.flip()
        clock.tick(60)
        if active and all(c.done for c in active):
            return tuple(c.baseline if c.done else None for c in cals)


def draw_ball(pygame, screen, pos):
    for i in range(6, 0, -1):
        r = BALL_RADIUS + i * 8
        surf = pygame.Surface((r * 2, r * 2), pygame.SRCALPHA)
        pygame.draw.circle(surf, (*GLOW, int(60 * i / 6)), (r, r), r)
        screen.blit(surf, (pos[0] - r, pos[1] - r))
    pygame.draw.circle(screen, BALL, pos, BALL_RADIUS)
    pygame.draw.circle(screen, (180, 230, 255),
                       (pos[0] - BALL_RADIUS // 3, pos[1] - BALL_RADIUS // 3), BALL_RADIUS // 4)


def draw_timer(pygame, screen, font, time_left, duration):
    w, _ = screen.get_size()
    cx, cy, r = w - 60, 60, 44
    pygame.draw.circle(screen, BAR_BG, (cx, cy), r)
    frac = time_left / duration
    colour = POS if frac > 0.4 else (255, 200, 50) if frac > 0.15 else NEG
    steps = max(1, int(60 * frac))
    for i in range(steps):
        a = math.pi / 2 - 2 * math.pi * frac * i / steps
        pygame.draw.line(screen, colour, (cx, cy),
                         (cx + int(r * math.cos(a)), cy - int(r * math.sin(a))), 3)
    pygame.draw.circle(screen, BG, (cx, cy), r - 8)
    label = font.render(f"{int(time_left) + 1}", True, TEXT)
    screen.blit(label, label.get_rect(center=(cx, cy)))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--axis", choices=("both", "horizontal", "vertical"), default="both")
    ap.add_argument("--port", default="AUTO")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--duration", type=float, default=30.0, help="seconds before auto-stop; 0 = no limit")
    ap.add_argument("--fs", type=float, default=None, help="sample rate (default: from the stream format)")
    args = ap.parse_args(argv)

    try:
        import pygame
    except ImportError:
        print("pygame is required for the visualiser: pip install pygame", file=sys.stderr)
        return 1
    try:
        port = eio.open_serial(args.port, args.baud)
    except eio.SerialError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    pygame.init()
    info = pygame.display.Info()
    screen = pygame.display.set_mode((min(1200, info.current_w - 100), min(700, info.current_h - 100)),
                                     pygame.RESIZABLE)
    pygame.display.set_caption("EOG Gaze Visualiser")
    fonts = {
        "normal": pygame.font.SysFont("Segoe UI", 22),
        "big": pygame.font.SysFont("Segoe UI", 42, bold=True),
        "dir": pygame.font.SysFont("Segoe UI", 60, bold=True),
        "small": pygame.font.SysFont("Segoe UI", 16),
    }
    clock = pygame.time.Clock()
    reader = SerialReader(port)
    reader.start()

    invert = True
    try:
        baselines = calibrate(pygame, reader, screen, fonts, args.axis)
        if baselines is None:
            return 0
        fs = args.fs or eio.NOMINAL_FS.get(reader.stream.format, 50.0)
        window = int(round(SMOOTHING_S * fs))
        smooth = [RollingMean(window), RollingMean(window)]
        norm = [0.0, 0.0]
        latest = [None, None]
        start = time.monotonic()
        running = True
        while running:
            time_left = args.duration - (time.monotonic() - start) if args.duration > 0 else None
            if (time_left is not None and time_left <= 0) or reader.stopped:
                break
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_ESCAPE, pygame.K_q):
                        running = False
                    elif event.key == pygame.K_i:
                        invert = not invert
                    elif event.key == pygame.K_r:
                        baselines = calibrate(pygame, reader, screen, fonts, args.axis)
                        if baselines is None:
                            running = False
                            break
                        for s in smooth:
                            s.reset()
                        start = time.monotonic()

            for sample in reader.drain():
                for i, value in enumerate(pick(sample, args.axis)):
                    if value is None or baselines[i] is None:
                        continue
                    latest[i] = value
                    diff = (baselines[i] - value) if invert else (value - baselines[i])
                    norm[i] = to_norm(smooth[i].feed(diff))

            w, h = screen.get_size()
            play_w, play_h = w - 2 * MARGIN, h - 2 * MARGIN - 80
            cx = w // 2 + int(norm[0] * play_w // 2)
            cy = (h - 80) // 2 + int(norm[1] * play_h // 2)
            cx = max(MARGIN + BALL_RADIUS, min(w - MARGIN - BALL_RADIUS, cx))
            cy = max(MARGIN + BALL_RADIUS, min(h - 80 - MARGIN - BALL_RADIUS, cy))

            primary = norm[1] if args.axis == "vertical" else norm[0]
            names = ("UP", "DOWN") if args.axis == "vertical" else ("LEFT", "RIGHT")
            if primary < -0.05:
                label, colour = names[0], NEG
            elif primary > 0.05:
                label, colour = names[1], POS
            else:
                label, colour = "CENTRE", TEXT

            screen.fill(BG)
            pygame.draw.line(screen, FAINT, (w // 2, MARGIN), (w // 2, h - 80 - MARGIN), 1)
            pygame.draw.line(screen, FAINT, (MARGIN, (h - 80) // 2), (w - MARGIN, (h - 80) // 2), 1)
            pygame.draw.rect(screen, FAINT, (MARGIN, MARGIN, play_w, play_h), 1, border_radius=12)
            draw_ball(pygame, screen, (cx, cy))
            text = fonts["dir"].render(label, True, colour)
            screen.blit(text, text.get_rect(center=(w // 2, h - 52)))
            if time_left is not None:
                draw_timer(pygame, screen, fonts["normal"], time_left, args.duration)
            hint = fonts["small"].render("R = recalibrate   I = flip direction   ESC = quit", True, (80, 80, 100))
            screen.blit(hint, (8, h - 22))
            status = fonts["small"].render(
                f"raw H:{latest[0]} V:{latest[1]}  baselines:{baselines}  invert:{invert}  (ADC counts)",
                True, (70, 70, 90))
            screen.blit(status, (8, 8))
            pygame.display.flip()
            clock.tick(60)

        if reader.stopped:
            print(f"Stream ended: {reader.stream.stats.stop_reason}")
    finally:
        port.close()
        pygame.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
