# 福建移动央视直播列表

在 Apple TV 的 APTV「远程配置」中添加以下订阅。如果家中宽带暂时无法访问 GitHub Raw，也可以下载 [`cctv.m3u`](cctv.m3u)，用 APTV「本地配置」导入。

```text
https://raw.githubusercontent.com/AkiraNyx/cctv-iptv/main/cctv.m3u
```

主列表固定为 CCTV-1 至 CCTV-17、CCTV-5+、CCTV-4K 和 CCTV-16 4K。首版 20 路于 2026-11-20 在厦门移动 IPv4 上通过解码。两路 4K 实测为 2160p25、H.264 8 bit，分别约 15.01 和 16.01 Mbps。详细测量值见 [`baseline.json`](baseline.json)。节目是否原生 4K 和跨网络的可用性均待验证。 [上游福建移动线路](https://github.com/xuanle6/IPTV/blob/a47ab835200b80a615f974b6bceef7c2f82978cf/yd.txt)

## 每日检查

GitHub Actions 在北京时间每天 02:23 触发，也可在 Actions 中手动运行。它校验主列表，从两个公开列表中抽取同频道候选，每个上游每频道最多测三条。通过 HLS 分片和 FFmpeg 严格视频解码后，按实际分辨率、HDR 标记、帧率或场率及视频码率为候选排序。

最新云端报告保存在 [`status.json`](status.json)。`higher_than_snapshot` 只表示本次云端样本的技术参数排序高于福建移动快照。候选仍需在福建移动上复测，才能手动更新主列表及对应的实测数据。云端超时或无法解码记为未验证。短样本无法确认节目制作规格或长时间播放稳定性。
