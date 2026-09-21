"""一次性研究串流：下載、顯存測試、微調、評估；只有排程讓路會自動等待續跑。"""
import argparse
import logging
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .main import ROOT, process_lock
from .research_data import write_json
from .training import TAIPEI, schedule_window

LOG = logging.getLogger(__name__)


def run(dataset: Path, output: Path, cutoff: str, activate: bool):
    """按既定順序執行一次研究；一般錯誤停止，狀態與完整子程序輸出留在本機。"""
    dataset.mkdir(parents=True, exist_ok=True)
    state_path = dataset / 'pipeline.json'
    with process_lock(dataset / 'pipeline.lock'):
        for stage in ('prepare', 'smoke', 'train', 'evaluate'):
            if stage in ('smoke', 'train') and (output / 'training.json').exists():
                continue
            while True:
                write_json(state_path, dict(stage=stage, state='running', updated_at=datetime.now(TAIPEI).isoformat()))
                command = [sys.executable, '-u', '-m', 'src.main', 'train' if stage == 'smoke' else stage,
                           '--dataset', str(dataset)]
                if stage == 'prepare':
                    command += ['--cutoff', cutoff]
                else:
                    command += ['--output', str(output)]
                if stage == 'smoke':
                    command += ['--smoke-steps', '2']
                if stage == 'train' and (output / 'resume.pt').exists():
                    command += ['--resume']
                if stage == 'evaluate' and activate:
                    command += ['--activate']
                LOG.info('研究階段 %s', stage)
                code = subprocess.run(command, cwd=ROOT, check=False).returncode
                if code == 0:
                    break
                if code == 1 and stage in ('smoke', 'train') and (output / 'training-fallback.json').exists():
                    fallback = json.loads((output / 'training-fallback.json').read_text())
                    active = json.loads((output / 'training-config.json').read_text())
                    # 加速設定只允許回退一次；原設定仍失敗就停止，避免無限重跑。
                    if active != fallback:
                        write_json(output / 'training-config.json', fallback)
                        write_json(output / 'training-fallback-used.json', dict(previous=active, fallback=fallback,
                                   updated_at=datetime.now(TAIPEI).isoformat()))
                        LOG.warning('加速訓練失敗；回退原設定並從最近checkpoint續跑')
                        continue
                if code != 75:
                    write_json(state_path, dict(stage=stage, state='failed', exit_code=code,
                                              updated_at=datetime.now(TAIPEI).isoformat()))
                    raise RuntimeError(f'{stage} 未完成，退出碼 {code}；修復後重跑同一命令')
                write_json(state_path, dict(stage=stage, state='paused_for_daily',
                                          updated_at=datetime.now(TAIPEI).isoformat()))
                while schedule_window() or (output / 'pause.request').exists():
                    time.sleep(30)
        write_json(state_path, dict(stage='complete', state='complete', activated=activate,
                                  updated_at=datetime.now(TAIPEI).isoformat()))


def main():
    """解析一次性研究參數；不建立週期性訓練排程或直接發送通知。"""
    parser = argparse.ArgumentParser(description='台股一次性研究工作')
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cutoff', required=True)
    parser.add_argument('--activate', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        run(args.dataset.resolve(), args.output.resolve(), args.cutoff, args.activate)
    except (ValueError, RuntimeError) as exc:
        LOG.error('%s', exc)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
