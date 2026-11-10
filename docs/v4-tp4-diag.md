# A5 V4 FA 四 worker 对照

```bash
curl -fL https://raw.githubusercontent.com/NaganooMei/file-transfer/main/scripts/v4_tp4_diag.sh -o /workspace/v4_tp4_diag.sh
bash /workspace/v4_tp4_diag.sh /workspace/unified-cache-management /mnt/ucm
```

使用目标服务的 Python 环境。脚本只依次调用仓库已有的三个测试，不修改 UCM 或 metrics，不重编译。要求实际安装 Cache v2；默认使用当前可见逻辑 NPU 0–3，不修改 CPU 绑核或 NUMA 策略。

| 测试 | 每 worker 读取 Posix | 每 worker 拷贝到 HBM |
| --- | --- | --- |
| Posix AIO | 128 block，每 block 3,186,688 字节 | 无 |
| Cache hit | 无，先预填共享 Cache | 512 block，每 block 3,183,872 字节 |
| Posix hit | 128 block，每 block 3,186,688 字节 | 512 block，每 block 3,183,872 字节 |

两组 Cache 均为 MLA 共享，总容量 16 GiB，plain 拷贝、4 stream，关闭 SDMA Direct 和 IO 聚合。V4 profile 仅模拟跨层打包的 FA：每 block 83 个 tensor，每 worker 每轮一个前端 Load task；不含 WA、模型 forward 或真实 TP 通信。

三组均运行 6 轮 Load。Cache 的前 1 轮预热、后 5 轮测量。Posix AIO 先 Dump 一遍，再重复读取同一组文件；保留 epoch 0 以对比首次打开文件和后续 FD 缓存复用。Pipeline Posix 每轮换新 key，先预埋再启动空 Cache。统一 aio + O_DIRECT。

结果默认保存在当前目录的 `v4-tp4-results-时间戳-PID/`，第三个位置参数可以指定一个尚不存在的输出目录。终端只显示关键行，**将 `key-output.txt` 返回即可**。三份 `.log` 文件保留完整输出，失败时自动显示最后 20 行。

存储数据位于 `/mnt/ucm` 下本次新建的 `ucm-v4-tp4-*` 目录，约 10.64 GiB，不含已有文件；退出时只删除本次目录。SIGKILL 可能残留。不清 Page Cache，无需 root。

仅查看命令、不运行：

```bash
bash /workspace/v4_tp4_diag.sh --dry-run /workspace/unified-cache-management /mnt/ucm
```

通过 `PYTHON=/path/to/python` 可以选择 Python 环境。
