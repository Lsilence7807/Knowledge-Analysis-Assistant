# 文件：skills/demo_script/run.py
# 作用：P6 演示技能入口：统计给定目录下 CSV 的行数（无参数时只看当前目录）
# 阶段：P6 Skill 导入与执行
# 依赖：标准库 csv、sys、pathlib（技能脚本只能用标准库，跑在技能目录里）
import csv
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    directory = Path(argv[0]) if argv else Path.cwd()
    if not directory.is_dir():
        print(f"不是目录：{directory}")
        return 2
    for path in sorted(directory.glob("*.csv")):
        with path.open(encoding="utf-8", errors="replace", newline="") as handle:
            rows = max(0, sum(1 for _ in csv.reader(handle)) - 1)
        print(f"{path.name}: {rows} 行")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
