"""Local card preview. The plugin itself is loaded by MaiBot via plugin.py."""

import argparse
from pathlib import Path

from bili.config import CardConfig
from bili.models import Card
from bili.render import card_html, pillow_card


def main():
    parser = argparse.ArgumentParser(description="生成 Bilibili 卡片离线样例（不连接 B 站）")
    parser.add_argument("--output", default="docs/card-preview.png")
    parser.add_argument("--font", default="", help="中文字体文件路径")
    args = parser.parse_args()
    card = Card(
        id="demo",
        kind="图文",
        title="把喜欢的更新，及时带到聊天里。",
        author="Bilibili 动态订阅",
        uid="123456",
        text="关注的 UP 更新了！\n\n动态、视频和直播消息，自动送到当前会话。\n"
        "在聊天中分享 B 站链接，也能展开成清晰的卡片。\n\n这是离线演示数据，非真实动态。",
        timestamp=1791691200,
        url="https://www.bilibili.com",
        stats=["动态订阅", "直播提醒", "链接解析"],
        original=Card(
            id="demo-original",
            kind="动态",
            title="每一条更新，都有出处。",
            author="MaiBot",
            text="保留作者、原文和链接，支持转发动态、内容过滤与发送重试。",
        ),
    )
    config = CardConfig(font_path=args.font)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(pillow_card(card, config, {}))
    output.with_suffix(".html").write_text(card_html(card, config, {}), encoding="utf-8")
    print(f"已生成：{output.resolve()}")


if __name__ == "__main__":
    main()
