# 文件传输

用于跨环境传递脚本、说明文档和小型文件，方便在另一台机器直接下载使用。

## 文件索引

| 文件 | 用途 |
| --- | --- |
| [v4_tp4_diag.sh](scripts/v4_tp4_diag.sh) | A5 V4 四 worker：Posix AIO、Cache 命中与 Posix 命中对照，截取关键输出 |
| [V4 对照说明](docs/v4-tp4-diag.md) | 下载命令、测试规模、分段指标和结果文件 |
| [cache2_hugetlb_160g.py](scripts/cache2_hugetlb_160g.py) | 160GiB Cache2 NUMA / 2MiB 大页回退四场景验证，包含检测、清零、分配与恢复 |
| [验证说明](docs/cache2-hugetlb-160g-validation.md) | 环境要求、运行命令、结果判定和恢复方式 |

## 下载大页验证脚本

```bash
curl -fL \
  https://raw.githubusercontent.com/NaganooMei/file-transfer/main/scripts/cache2_hugetlb_160g.py \
  -o cache2_hugetlb_160g.py

# 仅打印大页检测、清零和分配命令，不执行。
python3 cache2_hugetlb_160g.py --commands
```

实际执行需在已安装 UCM 对应修复版本的 Linux NPU 环境中进行：

```bash
sudo -E "$(command -v python3)" cache2_hugetlb_160g.py \
  --repo /path/to/unified-cache-management \
  --numa-nodes 0,1,2,3,4,5,6,7 \
  --run
```

节点列表按实际机器修改；rank 数由列表长度决定。完整说明见上方文档。

运行时实时显示逐节点大页配置、benchmark 输出和退出结果；完整 benchmark 日志仍保存到结果目录，安静运行时每 30 秒打印一次存活提示。

## 目录约定

- `scripts/`：可独立下载使用的脚本。
- `docs/`：运行说明和文件背景。
- `tests/`：不依赖 GPU/NPU 的脚本逻辑测试。

本地逻辑检查：

```bash
python3 -m pip install pytest
python3 -m pytest -q tests
```
