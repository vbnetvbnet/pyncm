# -*- coding: utf-8 -*-
# PyNCM CLI interface

from pyncm import (
    DumpSessionAsString,
    GetCurrentSession,
    LoadSessionFromString,
    SetCurrentSession,
    __version__
)
from pyncm.utils.lrcparser import LrcParser
from pyncm.utils.helper import TrackHelper, FuzzyPathHelper, SubstituteWithFullwidth
from pyncm.apis import login, track, playlist, album

from queue import Queue
from concurrent.futures import ThreadPoolExecutor
from threading import Thread
from time import sleep
from os.path import join, exists
from os import remove, makedirs

from logging import exception, getLogger, basicConfig
import sys, argparse, re , os
logger = getLogger('pyncm.main')
# Import checks
OPTIONALS = {"mutagen": False, "tqdm": False, "coloredlogs": False}
OPTIONALS_MISSING_INFO = {
    "mutagen": "无法为下载的音乐添加歌手信息，封面等资源",
    "tqdm": "将不会显示下载进度条",
    "coloredlogs": "日志不会以彩色输出",
}
from importlib.util import find_spec

for import_name in OPTIONALS:
    OPTIONALS[import_name] = find_spec(import_name)
    if not OPTIONALS[import_name]:
        sys.stderr.writelines(
            [f"[WARN] {import_name} 没有安装，{OPTIONALS_MISSING_INFO[import_name]}\n"]
        )

__desc__ = """PyNCM 网易云音乐下载工具 %s""" % __version__

# Key-Value classes
class BaseKeyValueClass:
    def __init__(self, **kw) -> None:
      for k, v in kw.items():
            self.__setattr__(k, v)


class TaskPoolExecutorThread(Thread):
    @staticmethod
    def tag_audio(track: TrackHelper, file: str, cover_img: str = ""):
        if not OPTIONALS["mutagen"]:
            return
        def write_keys(song):
            # Writing metadata
            # Due to different capabilites of containers, only
            # ones that can actually be stored will be written.
            complete_metadata = {
                "title" : [track.TrackName,*(track.TrackAliases or [])],
                "artist" : [*(track.Artists or [])],
                "albumartist" : [*(track.Album.AlbumArtists or [])],
                "album" : [track.AlbumName],
                "tracknumber" :  "%s/%s" % (track.TrackNumber,track.Album.AlbumSongCount),
                "date" : [str(track.Album.AlbumPublishTime)], # TrackPublishTime is not very reliable!
                "copyright": [track.Album.AlbumCompany],
                "discnumber" : [track.CD],
                # These only applies to vorbis tags (e.g. FLAC)                
                "totaltracks" : [str(track.Album.AlbumSongCount)],
                "ncm-id":[str(track.ID)]
            }                    
            for k,v in complete_metadata.items():
                try:
                    song[k] = v
                except:
                    pass
            song.save()

        def mp4():
            from mutagen import easymp4
            from mutagen.mp4 import MP4, MP4Cover

            song = easymp4.EasyMP4(file)
            write_keys(song)
            if exists(cover_img):
                song = MP4(file)
                song["covr"] = [MP4Cover(open(cover_img, "rb").read())]
                song.save()

        def mp3():
            from mutagen.mp3 import EasyMP3,HeaderNotFoundError
            from mutagen.id3 import ID3, APIC
            try:
                song = EasyMP3(file)            
            except HeaderNotFoundError:
                song = EasyMP3()
                song.filename = file
                song.save()
                song = EasyMP3(file)            
            write_keys(song)
            if exists(cover_img):
                song = ID3(file)
                song.update_to_v23()  # better compatibility over v2.4
                song.add(
                    APIC(
                        encoding=3,
                        mime="image/jpeg",
                        type=3,
                        desc="",
                        data=open(cover_img, "rb").read(),
                    )
                )
                song.save(v2_version=3)

        def flac():
            from mutagen.flac import FLAC, Picture
            from mutagen.mp3 import EasyMP3
            song = FLAC(file)            
            write_keys(song)
            if exists(cover_img):
                pic = Picture()
                pic.data = open(cover_img, "rb").read()
                pic.mime = "image/jpeg"
                song.add_picture(pic)   
                song.save()

        def ogg():
            import base64
            from mutagen.flac import Picture
            from mutagen.oggvorbis import OggVorbis

            song = OggVorbis(file)            
            write_keys(song)
            if exists(cover_img):
                pic = Picture()
                pic.data = open(cover_img, "rb").read()
                pic.mime = "image/jpeg"
                song["metadata_block_picture"] = [
                    base64.b64encode(pic.write()).decode("ascii")
                ]
                song.save()

        format = file.split(".")[-1].upper()
        for ext, method in [
            ({"M4A", "M4B", "M4P", "MP4"}, mp4),
            ({"MP3"}, mp3),
            ({"FLAC"}, flac),
            ({"OGG", "OGV"}, ogg),
        ]:
            if format in ext:
                return method() or True
        return False

    def download_by_url(self, url, dest, xfer=False):
        # Downloads generic content
        response = GetCurrentSession().get(url, stream=True)
        length = int(response.headers.get("content-length"))

        with open(dest, "wb") as f:
            for chunk in response.iter_content(128 * 2**10):
                self.xfered += len(chunk)
                if xfer:
                    self.finished_tasks += len(chunk) / length  # task [0,1]
                f.write(chunk)  # write every 128KB read
        return dest

    def __init__(self, *a, max_workers=4, **k):
        super().__init__(*a, **k)
        self.daemon = True
        self.finished_tasks: float = 0
        self.xfered = 0
        self.task_queue = Queue()
        self.max_workers = max_workers

    def run(self):
        def execute(task: BaseKeyValueClass):
            if type(task) == MarkerTask:
                # Mark a finished task w/o execution                
                self.finished_tasks += 1
                return
            if type(task) == TrackDownloadTask:
                try:
                    # Downloding source audio
                    dAudio = track.GetTrackAudioV1(task.audio.id, level=task.audio.level)
                    dAudio = dAudio.get("data", [{"url": ""}])[0]  # Dummy fallback value                
                    assert dAudio["url"], "%s 无法下载，资源不存在" % task.song.Title                
                    logger.info(
                        "开始下载 #%d / %d - %s - %s - %skbps - %s"
                        % (
                            task.index + 1,
                            task.total,
                            task.song.Title,
                            task.song.AlbumName,
                            dAudio["br"] // 1000,
                            dAudio["type"].upper(),
                        )
                    )
                    if not exists(task.audio.dest):
                        makedirs(task.audio.dest)
                    dest_src = self.download_by_url(
                        dAudio["url"],                        
                        task.save_as + "." + dAudio["type"].lower(),                    
                        xfer=True,
                    )
                    # Downloading cover
                    dest_cvr = self.download_by_url(
                        task.cover.url, task.save_as + '.jpg'
                    )
                    # Downloading & Parsing lyrics
                    dest_lrc = task.save_as + '.lrc'
                    lrc = LrcParser()
                    dLyrics = track.GetTrackLyrics(task.lyrics.id)
                    for k in set(dLyrics.keys()) & (
                        {"lrc", "tlyric", "romalrc"} - task.lyrics.lrc_blacklist
                    ):  # Filtering LRCs
                        lrc.LoadLrc(dLyrics[k]["lyric"])
                    lrc_text = lrc.DumpLyrics()
                    if lrc_text:
                        open(dest_lrc, "w", encoding="utf-8").write(lrc_text)
                    # Tagging the audio
                    try:
                        self.tag_audio(task.song, dest_src, dest_cvr)
                    except Exception as e:
                        logger.warning("标签失败 - %s - %s" % (task.song.Title, e))
                    logger.info(
                        "完成下载 #%d / %d - %s" % (task.index + 1, task.total, task.song.Title)
                    )                
                except Exception as e:
                    logger.warning("下载失败 %s - %s" % (task.song.Title, e))
                    task.routine.result_exception(task.song.ID,e,task.song.Title)
                # Cleaning up
                remove(dest_cvr)

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            while True:
                task = self.task_queue.get()
                future = executor.submit(execute, task)
                future.add_done_callback(lambda future: self.task_queue.task_done())


# Subroutines
class Subroutine:
    """Generic subroutine

    Subroutines are `callable`,upon called with `ids`,one
    queues tasks with all given arguments via `put_func` callback
    """
    exceptions = None
    def __init__(self, args, put_func) -> None:
        self.args = args
        self.put = put_func
        self.exceptions = dict()

    def result_exception(self,result_id,exception : Exception,desc=None):
        self.exceptions.setdefault(result_id,list())
        self.exceptions[result_id].append((exception,desc))

    @property
    def has_exceptions(self):
        return len(self.exceptions) > 0
    
class BaseDownloadTask(BaseKeyValueClass):
    id: int
    url: str
    dest: str
    level : str


class LyricsDownloadTask(BaseDownloadTask):
    id: int
    dest: str
    lrc_blacklist: set


class TrackDownloadTask(BaseKeyValueClass):
    song: TrackHelper
    cover: BaseDownloadTask
    lyrics: BaseDownloadTask
    audio: BaseDownloadTask

    index: int
    total: int
    lyrics_exclude: set
    save_as: str

    routine : Subroutine

class MarkerTask(BaseKeyValueClass):
    pass

class Playlist(Subroutine):
    """Base routine for ID-based tasks"""
    def forIds(self, ids):
        dDetails = [track.GetTrackDetail(ids[index:min(len(ids),index+1000)]).get("songs") for index in range(0,len(ids),1000)]
        dDetails = [song for stripe in dDetails for song in stripe]           
        dDetails = sorted(dDetails, key=lambda song: song["id"])
        index = 0
        for index, dDetail in enumerate(dDetails):
            try:
                song = TrackHelper(dDetail)
                output_name=SubstituteWithFullwidth(
                    self.args.output_name.format(**song.template)
                )
                output_folder=self.args.output.format(**{
                    k:SubstituteWithFullwidth(v) for k,v in song.template.items()
                })
                # If audio file already exsists
                # Skip the entire download if `--no_overwrite` is explicitly set                            
                if self.args.no_overwrite:
                    if FuzzyPathHelper(output_folder).exists(
                        output_name,partial_extension_check=True
                    ):
                        logger.warning(
                            "单曲 #%d / %d - %s - %s 已存在，跳过"
                            % (index + 1, len(dDetails), song.Title, song.AlbumName))
                        self.put(MarkerTask())
                        continue                    
                tSong = TrackDownloadTask(
                    index=index,
                    total=len(dDetails),
                    song=song,
                    cover=BaseDownloadTask(
                        id=song.ID, url=song.AlbumCover, dest=output_folder
                    ),
                    audio=BaseDownloadTask(
                        id=song.ID,
                        level=self.args.quality,
                        dest=output_folder,
                    ),
                    lyrics=LyricsDownloadTask(
                        id=song.ID,
                        dest=output_folder,
                        lrc_blacklist=set(self.args.lyric_no),
                    ),
                    save_as=join(output_folder,output_name),
                    routine=self
                )                

                self.put(tSong)

            except Exception as e:                    
                logger.warning(
                    "单曲 #%d / %d - %s - %s 无法下载： %s"
                    % (index + 1, len(dDetails), song.Title, song.AlbumName, e)
                )                
                self.result_exception(song.ID,e,song.Title)
        return index + 1

    def __call__(self, ids):
        queued = 0
        for _id in ids:
            dList = playlist.GetPlaylistInfo(_id)
            logger.info("歌单 ：%s" % dict(dList)["playlist"]["name"])
            queued += self.forIds(
                [tid.get("id") for tid in dict(dList)["playlist"]["trackIds"]]
            )
        return queued

class Album(Playlist):
    def __call__(self, ids):
        queued = 0
        for _id in ids:
            dList = album.GetAlbumInfo(_id)
            logger.info("专辑 ：%s" % dict(dList)["album"]["name"])
            queued += self.forIds([tid["id"] for tid in dList["songs"]])
        return queued

class Song(Playlist):
    def __call__(self, ids):
        return self.forIds(ids)
    
def create_subroutine(sub_type) -> Subroutine:
    """Dynamically creates subroutine callable by string specified"""
    return {"song": Song, "playlist": Playlist, "album": Album}[sub_type]


def parse_sharelink(url):
    """Parses (partial) URLs for NE resources and determines its ID and type

    e.g.
        31140560 (plain song id)
        https://greats3an.github.io/pyncmd/?trackId=1818064296 (pyncmd)
        分享Ali Edwards的单曲《Devil Trigger》: http://music.163.com/song/1353163404/?userid=6483697162 (来自@网易云音乐) (mobile app)
        "分享mos9527创建的歌单「東方 PC」: http://music.163.com/playlist?id=72897851187" (desktop app)
    """
    rurl = re.findall("(?:http|https):\/\/.*", url)
    if rurl:
        url = rurl[0]  # Use first URL found. Otherwise use value given as is.
    numerics = re.findall("\d{4,}", url)
    assert numerics, "未在链接中找到任何 ID"
    ids = numerics[:1]  # Only pick the first match
    table = {
        "song": ["trackId", "song"],
        "playlist": ["playlist"],
        "album": ["album"],
    }
    rtype = "song"  # Defaults to songs (tracks)
    for rtype_, rkeyword in table.items():
        for kw in rkeyword:
            if kw in url:
                rtype = rtype_
                break  # Match type by keyword
    return rtype, ids


PLACEHOLDER_URL = "00000"


def parse_args():
    """Setting up __main__ argparser"""
    parser = argparse.ArgumentParser(
        description=__desc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "url", metavar="链接", help="网易云音乐分享链接", nargs="*", default=PLACEHOLDER_URL
    )
    group = parser.add_argument_group("下载")
    group.add_argument(
        "--max-workers", "-m", metavar="最多同时下载任务数", default=4, type=int
    )
    group.add_argument(
        "--output-name",
        "--template",
        "-t",
        metavar="保存文件名模板",
        help=r"""保存文件名模板
    参数：    
        id     - 网易云音乐资源 ID
        year   - 出版年份
        no     - 专辑中编号
        album  - 专辑标题
        track  - 单曲标题        
        title  - 完整标题
        artists- 艺术家名
    例：
        {track} - {artists} 等效于 {title}""",
        default=r"{title}",
    )
    group.add_argument("-o","--output", metavar="输出", default=".", help=r"""输出文件夹
    注：该参数也可使用模板，格式同 保存文件名模板"""
    )
    group.add_argument(
        "--quality",
        metavar="音质",
        choices=['standard','exhigh','lossless','hires'],
        help=r"""音频音质（高音质需要 CVIP）
    参数：
        hires  - Hi-Res
        lossless- “无损”
        exhigh  - 较高
        standard- 标准""",
        default="standard",
    )
    group.add_argument("--no-overwrite", action="store_true", help="不重复下载已经存在的音频文件")
    group = parser.add_argument_group("歌词")
    group.add_argument(
        "--lyric-no",
        metavar="跳过歌词",
        help=r"""跳过某些歌词类型的合并
    参数：
        lrc    - 源语言歌词
        tlyric - 翻译后歌词
        romalrc- 罗马音歌词
    例：
        --lyric-no tlyric --lyric-no romalrc 将只下载源语言歌词""",
        choices=["lrc", "tlyric", "romalrc"],
        default="",
        nargs="+",
    )
    group = parser.add_argument_group("登陆")
    group.add_argument("--phone", metavar="手机", default="", help="网易账户手机号")
    group.add_argument("--pwd", "--password", metavar="密码", default="", help="网易账户密码")
    group.add_argument("--save", metavar="[保存到]", default="", help="写本次登录信息于文件")
    group.add_argument(
        "--load", metavar="[保存的登陆信息文件]", default="", help="从文件读取登录信息供本次登陆使用"
    )
    group.add_argument("--http", action="store_true", help="优先使用 HTTP，不保证不被升级")
    group.add_argument("--log-level", help="日志等级", default="NOTSET")

    args = parser.parse_args()
    
    try:
        return args , [parse_sharelink(url) for url in args.url]
    except AssertionError:
        if args.url == PLACEHOLDER_URL:
            sys.argv.append("-h")  # If using placeholder, no argument is really passed
            return __main__()  # In which case, print help and exit
        assert args.save, "无效分享链接 %s" % ' '.join(args.url) # Allow invalid links for this one            
        return args , []

def __main__():    
    args , tasks = parse_args()    
    log_stream = sys.stdout
    # Getting tqdm & logger to work nicely together
    if OPTIONALS["tqdm"]:
        from tqdm.std import tqdm as tqdm_c

        class SemaphoreStdout:
            @staticmethod
            def write(__s):
                # Blocks tqdm's output until write on this stream is done
                # Solves cases where progress bars gets re-rendered when logs
                # spews out too fast
                with tqdm_c.external_write_mode(file=sys.stdout, nolock=False):
                    return sys.stdout.write(__s)

        log_stream = SemaphoreStdout
    if args.log_level == 'NOTSET':
        args.log_level = os.environ.get('PYNCM_DEBUG','INFO')
    if OPTIONALS["coloredlogs"]:
        import coloredlogs

        coloredlogs.install(
            level=args.log_level,
            fmt="%(asctime)s %(name)s [%(levelname).4s] %(message)s",
            stream=log_stream,
            isatty=True,
        )
    basicConfig(
        level=args.log_level, format="[%(levelname).4s] %(name)s %(message)s", stream=log_stream
    )
    if args.load:
        logger.info("读取登录信息 : %s" % args.load)
        SetCurrentSession(LoadSessionFromString(open(args.load).read()))
    if args.http:
        GetCurrentSession().force_http = True
        logger.warning("优先使用 HTTP")
    if args.phone and args.pwd:
        login.LoginViaCellphone(args.phone, args.pwd)
        logger.info(
            "账号 ：%s (VIP %s)"
            % (GetCurrentSession().nickname, GetCurrentSession().vipType)
        )
    if args.save:
        logger.info("保存登陆信息于 : %s" % args.save)
        open(args.save, "w").write(DumpSessionAsString(GetCurrentSession()))
        return 0
    if not GetCurrentSession().logged_in:
        login.LoginViaAnonymousAccount()
        logger.info("以匿名身份登陆成功，UID: %s" % GetCurrentSession().uid)
    executor = TaskPoolExecutorThread(max_workers=args.max_workers)
    executor.daemon = True
    executor.start()

    def enqueue_task(task):
        executor.task_queue.put(task)

    total_queued = 0
    subroutines = []
    for rtype,ids in tasks:
        task_desc = "ID: %s 类型: %s" % (ids[0],{'album':'专辑','playlist':'歌单®','song':'单曲'}[rtype])
        logger.info("处理任务 %s" % task_desc) 
        subroutine = create_subroutine(rtype)(args, enqueue_task)
        total_queued += subroutine(ids)  # Enqueue tasks
        subroutines.append((subroutine,task_desc))

    if OPTIONALS["tqdm"]:
        import tqdm

        _tqdm = tqdm.tqdm(
            bar_format="{desc}: {percentage:.1f}%|{bar}| {n:.2f}/{total_fmt} {elapsed}<{remaining}"
        )
        _tqdm.total = total_queued

        def report():
            _tqdm.desc = _tqdm.format_sizeof(executor.xfered, suffix="B", divisor=1024)
            _tqdm.update(min(executor.finished_tasks, total_queued) - _tqdm.n)
            return True

    else:

        def report():
            sys.stderr.write(
                f"下载中 : {executor.finished_tasks:.1f} / {total_queued} ({(executor.finished_tasks * 100 / total_queued):.1f} %,{executor.xfered >> 20} MB)               \r"
            )
            return True

    while executor.task_queue.unfinished_tasks >= 0:
        try:
            report() and sleep(0.5)
            if executor.task_queue.unfinished_tasks == 0:
                break
        except KeyboardInterrupt:
            break
    
    # Check final results
    # Thought: Maybe we should automatically retry these afterwards?
    failed_ids = dict()
    for routine,desc in subroutines:
        routine : Subroutine
        if routine.has_exceptions:
            logger.error('%s - 下载未完成' % desc)
            for exception_id , exceptions in routine.exceptions.items():
                failed_ids[exception_id] = True
                for exception in exceptions:
                    exception_obj, desc = exception
                    logger.warning('下载出错 ID: %s - %s%s' % (exception_id,exception_obj,' (%s)' % desc if desc else ''))
    if failed_ids:
        logger.error('你可以将下载失败的 ID 作为参数以再次下载')
        logger.error('所有失败的任务 ID: %s' % ' '.join([str(i) for i in failed_ids.keys()]))
    report()
    logger.info(f'任务完成率 {(executor.finished_tasks * 100 / total_queued):.1f}%')
    return 0


if __name__ == "__main__":
    sys.exit(__main__())
