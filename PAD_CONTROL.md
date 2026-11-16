# 虚拟手柄控制手册

> 守护进程常驻后，你和脚本都能直接控制手柄。
> **谁需要操作，直接说。**

## 启动守护进程（只需一次，重启会暂停游戏）

```powershell
cd <项目根目录>
& python tools\pad_daemon.py
```

> ⚠ 启动/重启守护进程 = 设备断连重连 = **游戏自动暂停**，需要按一次 `start` 恢复。

## 按键

```powershell
$py = "python"

& $py tools\padkey.py a                    # 按一次 A
& $py tools\padkey.py a a a                # 连按 3 次（间隔 0.5s）
& $py tools\padkey.py down*5 a             # 下按 5 次再按 A
& $py tools\padkey.py --gap 0.8 a a        # 加长间隔到 0.8s
& $py tools\padkey.py start                # Start（暂停/继续）
& $py tools\padkey.py back                 # Back/B 返回
& $py tools\padkey.py --status             # 查看守护进程状态
```

## 摇杆

```powershell
& $py tools\padkey.py --stick 0.9 0 --secs 1.5    # 右摇杆推右 1.5 秒
& $py tools\padkey.py --stick 0 0.9 --secs 1.0    # 推上
& $py tools\padkey.py --stick -0.9 0 --secs 1.0   # 推左
& $py tools\padkey.py --stick 0 -0.9 --secs 1.0   # 推下
```

## 按键名对照

| 名称 | 含义 |
|---|---|
| `a` `b` `x` `y` | 面键 |
| `start` `back` `guide` | 功能键 |
| `lb` `rb` | 肩键（L1/R1） |
| `ls` `rs` | 摇杆按下（L3/R3） |
| `up` `down` `left` `right` | 十字键 |

别名：`l1`→`lb`，`r1`→`rb`，`select`→`back`，`ok`/`confirm`→`a`，`cancel`→`b`

## 底层管道（脚本用）

`tools\padctl.py` 或直接 `acvd.pipe.PipeClient("acvd_pad")`：

```
status                  查询
r <x> <y>               设置右摇杆（不阻塞）
rhold <x> <y> <secs>    推杆保持 N 秒后回中
press <name>            按一次
keys <gap> <hold> <k...> 批量按键
center                  回中
quit                    关闭守护进程
```

## 当前脚本控制权约定

- **我操作时**：跑采样/定位脚本，推杆用 `rhold`（精确、不阻塞）
- **你操作时**：用 `padkey.py` 手动按键/推杆
- 需要切换时**明说**即可
