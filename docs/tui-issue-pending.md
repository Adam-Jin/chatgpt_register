我对当前 TUI 实现（monitor/ 目录）做了一次走查，主要问题如下，按优先级排序：

  🔴 高优先级（线程安全 & 数据竞争）

  1. 共享状态没有锁保护 — monitor/app.py:186-197, 361-375
  _active_workers / _worker_states / _worker_history / _view_mode 同时被 Textual 主事件循环和
  on_mount() 启动的 daemon 线程读写。_drain_bus() 与 _kickoff_workers
  之间没有任何同步，长时间运行容易出现 RuntimeError: dictionary changed size during iteration 或脏读。
  → 用 threading.Lock，或把所有跨线程更新统一通过 self.call_from_thread(...) 调度回主线程。
  2. _suspend_log_follow_tracking 计数器不是原子的 — monitor/app.py:540-545
  += 1 / -= 1 在并发 coroutine 中可能交错，计数器会变负，导致 follow logs 行为异常。
  → 改用 threading.Lock 或 reactive 状态。
  3. 后台 _runner 线程未在 on_unmount 中 join — monitor/app.py:217-226
  仅靠 daemon 强杀，进程退出时可能在事件还在投递的瞬间被截断，留下半完成的数据。

  🟠 中优先级（错误处理 & 资源管理）

  4. except Exception 吞掉一切 — monitor/app.py:349-351
  _drain_bus 的 try/except Exception: break 会同时捕获 queue.Empty 之外的真实异常（甚至
  MemoryError），导致问题被静默隐藏。
  → 改成 except queue.Empty。
  5. 手动驱动 context manager — monitor/app.py:244-265
  _install_stream_capture 直接调用 __enter__/__exit__，中间任何一步抛错都会让 _restore_stream_capture
  操作半初始化对象。
  → 改用 contextlib.ExitStack。
  6. 清理逻辑重复且被 suppress(Exception) 包裹 — monitor/__init__.py:79-104
  _cleanup_bus / replay_buffer.stop() 在多个分支被重复调用，所有失败都被吞掉，调试时无从定位。
  → 加 _cleaned 标志位 + 真实异常落日志。

  🟡 设计/健壮性

  7. 事件队列容量与 drain 速率不匹配 — bus.py:63-72、app.py:173
  订阅队列固定 8192，_drain_bus 每 100ms 处理 200 条 ≈ 2000 条/秒；高峰会持续丢日志，且仅在
  dropped_events 计数，UI 上不显眼。
  → 关键级别（ERROR/WARN）单独通道，UI 状态栏高亮丢弃数。
  8. _pool_snapshot 用 setdefault — app.py:339-343
  首次写入后再也不会更新，pool 配置变更（max_reuse 等）UI 永远停留在旧值。
  → 直接赋值。
  9. _selected_worker_id 可能悬空 — app.py:452-454
  worker 被 _sync_worker_activity 移除后，_selected_worker_id 仍指向它，下游访问会 KeyError。
  → 移除 worker 时同步清/重选。
  10. 多版本 Textual 兼容的 _invoke_log_method — app.py:547-570
  多个签名盲试，全部失败时静默 return，掉日志难以排查。
  → 在 requirements.txt 钉死 textual 版本，删掉这层魔法。
  11. StreamCapture.write 丢弃空行 — fallback.py:131-143
  if line.strip() 会忽略空行，但 flush() 时却保留缓冲内容，行为不一致。
  12. int(summary.get("done", status.done)) 容易残留旧值 — app.py:296-303
  一旦 summary 暂时缺键，UI 会一直保留前一次的数字，看起来"卡住了"。
  → 默认 0，并在转换失败时记录。

  建议优先修复顺序

  1. 共享状态加锁（#1, #2）— 最容易引发偶发崩溃
  2. 缩小 except Exception 范围（#4, #6）— 让真实问题暴露
  3. ExitStack 重构 stream capture（#5）— 资源安全
  4. 队列丢弃可见化（#7）+ pool snapshot 修复（#8）— 用户体验