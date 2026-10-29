"""配置：端口、数据目录、各类常量和环境变量（SFD_*）。"""

import os
import sys


def _parse_port():
    try:
        return int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    except ValueError:
        return 8000


PORT = _parse_port()


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


DATA_DIR = os.environ.get("SFD_DATA_DIR") or BASE_DIR


SHARE_DIR = os.path.join(DATA_DIR, "shared")


TEXTS_FILE = os.path.join(DATA_DIR, "texts.json")


FILES_META = os.path.join(DATA_DIR, "files.json")


os.makedirs(SHARE_DIR, exist_ok=True)


IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "ico", "heic", "heif"}


VIDEO_EXTS = {"mp4", "mov", "m4v", "avi", "mkv", "webm", "3gp", "wmv", "flv"}


AUDIO_EXTS = {"mp3", "wav", "m4a", "aac", "flac", "ogg", "wma"}


MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS


LARGE_FILE_BYTES = 1024 ** 3       # 1GB：达到这个体积走"1天"过期


TTL_NORMAL_DAYS = 3                # 普通文件：上传满 3 天自动清除内容


TTL_LARGE_DAYS = 1                 # 大文件：上传满 1 天自动清除内容


GHOST_RETENTION_DAYS = 30          # 内容清除后，记录本身再保留 30 天没人重新分享就彻底删除


SWEEP_INTERVAL_SECONDS = 60        # 后台清理线程扫描间隔


LONGPOLL_SECONDS = 10              # /status 长轮询最长挂起时间，必须小于在线窗口


ONLINE_WINDOW_SECONDS = 14         # 心跳窗口：这段时间内有请求就算"在线"


FEED_PAGE_SIZE = 200               # 列表每页条数，更早的内容点"加载更多"


MAX_UPLOAD_BYTES = int(os.environ.get("SFD_MAX_UPLOAD_BYTES", 10 * 1024 ** 3))  # 单次上传上限，默认 10GB


PASSCODE = os.environ.get("SFD_PASSCODE", "")  # 设置后，非本机访问需要先输入口令


PRESENCE_PRUNE_SECONDS = 300       # 在线心跳记录的兜底清理窗口


ANIMAL_WORDS = [
    "浣熊", "水獭", "柯基", "熊猫", "狐狸", "考拉", "刺猬", "企鹅", "松鼠", "河马",
    "长颈鹿", "羊驼", "仓鼠", "海豚", "猫头鹰", "变色龙", "树懒", "犀牛", "斑马", "章鱼",
    "水母", "雪貂", "土拨鼠", "北极熊", "袋鼠", "蜂鸟", "小熊猫", "锦鲤", "海獭", "猎豹",
]


INLINE_SAFE_EXTS = (IMAGE_EXTS - {"svg"}) | VIDEO_EXTS | AUDIO_EXTS  # 其余类型强制下载，避免上传的 HTML/SVG 在本站源下执行脚本
