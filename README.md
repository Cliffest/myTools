# sync

## sync.py

以 source 目录为参考, 增量同步到 sync 目录下, 并记录每次同步的更改信息到 sync/log.txt.
可识别 source/.syncignore 下的忽略规则 (完整路径).

`source`: 源目录路径.

`sync`: 目标目录路径.

`-m`, `--mode`: 同步模式, date: 按最新修改日期增量同步, 
                         file: 比对文件内容增量同步, 
                         reset: 删除后复制源文件过去.

`-i`, `--interval`: 同步间隔时间(s), 0 表示仅执行一次; 默认为 0.

`-D`, `--delete`: 启用后，删除 sync 目录中匹配忽视规则的所有文件.

`-f`, `--time_factor`: 处理不同设备的时间精度, 应 =1/精度; 
                       操作系统间可设 1e6; U盘基于文件系统, 例如 exFAT 应设 1.