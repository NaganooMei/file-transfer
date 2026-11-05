# Cache2 NUMA 大页回退：160GiB 四场景验证

脚本：[`scripts/cache2_hugetlb_160g.py`](../scripts/cache2_hugetlb_160g.py)。在安装并重新编译了 [UCM PR #1609](https://github.com/ModelEngine-Group/unified-cache-management/pull/1609) 修复的 Linux NPU 环境中运行；不加载模型权重。

调用现有 `cache_bw.py`，使用 160GiB 总 Cache、1MiB whole-block、8 个数据块、1 个 epoch、`--verify` 和 plain copy。目的是验证内存初始化、NPU 注册、数据复制与退出，不作为服务性能基准。

## 四个场景

默认 8 rank，分别放到 NUMA node0..7，每 rank 20GiB。只操纵 **2MiB HugeTLB**：

| 场景 | 每个节点的大页池 | 期望 |
| --- | --- | --- |
| `01-enough` | 每节点 20GiB，即 10240 页 | 8 个段均为 `hugetlb-2m` |
| `02-no-huge` | 所有节点清零 | 8 个段均为 `base` |
| `03-partial-nodes` | node0..3 各 20GiB，其余清零 | 无大页节点回退；其余节点可以使用大页 |
| `04-local-insufficient` | 每节点 4GiB，即 2048 页，全局 32GiB | 全局够预留一个 20GiB 段，但每个目标节点不足，必须安全回退；日志需出现 `fallocate memfd failed` |

多个 rank 放在同一 NUMA 节点时，脚本按该节点的 rank 数计算充足池；不足组会调整池大小，保持“全局至少够一个 rank、每节点不足一个 rank”的条件。

第三组中，其他 rank 可能短暂占用全局 reservation，让有大页的节点也先行回退。因此不强制要求恰好 4 个大页段；`summary.json` 会记录每个 rank 的实际类型和 `mixed_backing_observed`，不会将安全回退误报成启动失败。

## 执行

把单个 Python 脚本复制到机器，例如 `/tmp/cache2_hugetlb_160g.py`。先查看节点、运行计划和大页操作命令：

```bash
numactl --hardware
python3 /tmp/cache2_hugetlb_160g.py --repo /path/to/unified-cache-management
python3 /tmp/cache2_hugetlb_160g.py --commands
```

`--commands` 只打印检测、清零、按节点分配大页的 `cat` / `echo` 命令，不执行。手工命令本身不负责恢复，正式验证推荐用 `--run`：

```bash
sudo -E "$(command -v python3)" /tmp/cache2_hugetlb_160g.py \
  --repo /path/to/unified-cache-management \
  --numa-nodes 0,1,2,3,4,5,6,7 \
  --output /tmp/cache2-hugetlb-160g-result \
  --run
```

在已经具备 root、CANN 环境和可写 sysfs 的容器内，直接使用其 `python3`，可省略 `sudo -E`。

`--numa-nodes` 是 rank0、rank1……对应的节点，不是节点集合；rank 数由列表长度决定。例如 8 rank / 4 个节点可用 `0,0,1,1,2,2,3,3`。此参数会显式传给 `cache_bw.py`，使内存落点和测试池一致。

执行前需保证测试卡和主机内存空闲。脚本会修改整台主机的 2MiB 大页池，并拒绝操作已有使用者或 reservation 的池；1GiB 池若非零，也会退出，避免其掩盖 2MiB 测试。不会修改 1GiB 池、THP 设置或停止其他人的进程。

## 检测、清零、分配命令示例

以下命令也写在脚本中，`--commands` 可打印全部节点和四个场景的对应命令。重定向需要在 root shell 内执行：

```bash
# 检测 node0：nr 是配置页数，free 是当前空闲页数。
cat /sys/devices/system/node/node0/hugepages/hugepages-2048kB/nr_hugepages
cat /sys/devices/system/node/node0/hugepages/hugepages-2048kB/free_hugepages

# 禁止按需超额分配，才能保证“无大页 / 不足”的条件成立。
echo 0 > /sys/kernel/mm/hugepages/hugepages-2048kB/nr_overcommit_hugepages

# 清零所有节点的空闲 2MiB 大页。
for p in /sys/devices/system/node/node*/hugepages/hugepages-2048kB/nr_hugepages; do
  echo 0 > "$p"
done

# 为 node0 分配 20GiB：10240 × 2MiB。
echo 10240 > /sys/devices/system/node/node0/hugepages/hugepages-2048kB/nr_hugepages

# 不足组为 node0 分配 4GiB：2048 × 2MiB。
echo 2048 > /sys/devices/system/node/node0/hugepages/hugepages-2048kB/nr_hugepages
```

分配后必须重新读取 `nr_hugepages`。内存碎片或空闲内存不足可能导致实际分配页数少于写入值；脚本会将其记为场景配置失败，并跳过该次 benchmark，不冒充 UCM 验证成功。

## 结果与恢复

输出目录包括 `hugepage-backup.json`、四份 case 日志和 `summary.json`。每个 case 要求 benchmark 退出 0、出现完整 Load summary、记录全部 rank 的页类型，并满足该组的预期。`--verify` 校验失败会使 benchmark 失败。

执行时会实时显示：

- 大页清零、分配和恢复的每次写入：完整 sysfs 路径、原值、目标值、实际值、空闲页数及耗时。`SET` 在写入前打印；若分配较慢，可直接看出停在哪个节点。
- `START benchmark`、实际执行命令和日志文件路径；benchmark 的 stdout/stderr 同时显示在终端并保存在对应 case 日志中。
- benchmark 每运行 30 秒打印一次存活提示，结束时打印退出码和耗时。

看到 `RUN` 后的 `SET` 表示正在配置大页；看到 `START benchmark` 才表示已启动 `cache_bw.py`。更新脚本后，新日志在下次启动时生效，已经运行的旧进程不会自动更新。

正常结束、超时、Ctrl+C 或 SIGTERM 均进入恢复流程；只终止本次脚本启动的进程组。请确认最后打印 `Original HugeTLB pools restored.`，且 `summary.json` 中 `restored` 为 `true`。脚本退出 0 代表四组和恢复均成功。

SIGKILL、断电等无法执行清理时，用备份恢复：

```bash
sudo -E "$(command -v python3)" /tmp/cache2_hugetlb_160g.py \
  --restore /tmp/cache2-hugetlb-160g-result/hugepage-backup.json
```

测试结束后回传 `summary.json` 和失败 case 的日志即可；出现 `207001` 等注册错误时，脚本会保留失败结果，不把它判成大页回退通过。
