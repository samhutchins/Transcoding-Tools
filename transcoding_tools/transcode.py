from argparse import ArgumentParser
from os.path import basename
from sys import exit
from subprocess import Popen, PIPE, run, DEVNULL, STDOUT, TimeoutExpired
from functools import reduce
from fractions import Fraction
import os
import re
import shlex
import sys

from .__init__ import __version__
from . import utils

version = f"""\
transcode.py {__version__}
Copyright (c) 2020,2021 Sam Hutchins\
"""

help = f"""\
Transcode Blu Ray and DVD rips to smaller, Plex friendly, versions.

Usage = transcode.py FILE [OPTION...]

Creates an `mkv` file in the current directory. The video will be converted to
h.264, averaging up to 8000kb/s (dependent on resolution). The first audio track
will be transcoded with up to 6 channels (5.1) at 640kb/s surround AC3, 192kb/s
stereo AAC, or 96kb/s mono AAC. Any subtitles in the same language as the main
audio will be included in their original format, and forced subtitles will be
burned in. If the input is interlaced it will be deinterlaced, and the video
will be cropped automatically to remove black bars. Track selection, cropping,
deinterlacing, and burning can be controlled by the options documented below.

Input options:
    --dry-run       print the HandBrakeCLI command and exit
    --start HH:MM:SS
                    The time in the input file to start at
    --stop HH:MM:SS
                    The time int he input file to stop at

Output options:
    --hevc          Output h.265 (hevc) instead of h.264. This will also reduce
                      the target bitrate
    --10-bit        Output 10 bit video

Encoder options:
    --target-bitrate big|small
                    Tweak the target bitrates. Use `big` for higher than default
                      bitrates, and `small` for lower ones.
    --hw-accel      Use a hardware encoder. These are much faster, but generally
                      lower quality
    --two-pass      Two-pass encoding
    --hrd           Encode an HRD compliant stream

Picture options:
    --crop TOP:BOTTOM:LEFT:RIGHT
                    Specify cropping values (default: auto detected)
    --no-crop       Disable cropping
    --deinterlace   Deinterlace the input (default: auto-applied on some inputs)
    --no-deinterlace
                    Disable deinterlacing
    --preserve-field-rate
                    Preserve field rate when deinterlacing. e.g., 50i -> 50p
    --par X:Y       Override the pixel aspect ratio (default: same as input)

Audio options:
    --audio TRACK[ TRACK...]|LANGUAGE[ LANGUAGE...]|all
                    Which audio tracks to include in the output. (default: 1)
    --stereo        Restrict the output to stereo

Subtitle options:
    --burn TRACK    Which subtitle track to burn into the video
                      (default: auto-applied for some inputs)
    --no-burn       Disable burning of subtitles
    --subtitles TRACK[ TRACK...]|LANGUAGE[ LANGUAGE...]|all
                    Which subtitle tracks to include in the output
                      (default: same language as main audio)
    --no-subtitles  Disable added subtitles

Other options:
    --skip-remux    Don't remux the output after transcoding
-d, --debug         print debug information
-h, --help          print this message and exit
    --version       print version information and exit

Requires `HandBrakeCLI`, `ffprobe`, `ffmpeg`, `mkvpropedit`, and `mkvmerge`\
"""

class Transcoder:
    def __init__(self):
        self.dry_run = False
        self.start_time = None
        self.stop_time = None
        
        self.two_pass = False

        self.crop = "auto"
        self.deinterlace = "auto"
        self.preserve_field_rate = False
        self.par = None

        self.stereo = False
        self.audio = ["1"]

        self.burned_sub = "auto"
        self.subtitles = "auto"

        self.skip_remux = False
        self.debug = False

        self.video_bitrate_index = 2
        self.video_bitrate_ladder = {
            "1080p": [4000, 6000, 8000, 12000],
            "720p": [2000, 3000, 4000, 6000],
            "sd": [1000, 1500, 2000, 3000]
        }

        self.encoder = None
        self.supported_encoders = {
            "x264": {
                "name": "x264",
                "type": "sw",
                "format": "avc",
                "encopts": "ratetol=inf:mbtree=0",
                "maxrate": 3,
                "bufsize": 3.75
            },
            "x265": {
                "name": "x265",
                "type": "sw",
                "format": "hevc",
                "encopts": "ctu=32:merange=25:weightb=1:aq-mode=1:cutree=0:deblock=-1,-1:selective-sao=2",
                "maxrate": 1.5,
                "bufsize": 2
            },
            "qsv_h264": {
                "name": "qsv_h264",
                "type": "hw",
                "format": "avc",
            },
            "qsv_h265": {
                "name": "qsv_h265",
                "type": "hw",
                "format": "hevc",
            },
            "nvenc_h264": {
                "name": "nvenc_h264",
                "type": "hw",
                "format": "avc",
                "encopts": "spatial-aq=1"
            },
            "nvenc_h265": {
                "name": "nvenc_h265",
                "type": "hw",
                "format": "hevc",
                "encopts": "spatial-aq=1:temporal-aq=1"
            },
            "vt_h264": {
                "name": "vt_h264",
                "type": "hw",
                "format": "avc",
            },
            "vt_h265": {
                "name": "vt_h265",
                "type": "hw",
                "format": "hevc",
            },
            "vce_h264": {
                "name": "vce_h264",
                "type": "hw",
                "format": "avc",
            },
            "vce_h265": {
                "name": "vce_h265",
                "type": "hw",
                "format": "hevc",
            }
        }

        self.available_video_encoders = []
        self.available_audio_encoders = []


    def run(self):
        parser = ArgumentParser(add_help=False)
        parser.add_argument("file", nargs="?")
        parser.add_argument("--scan", action="store_true")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--start", metavar="HH:MM:SS")
        parser.add_argument("--stop", metavar="HH:MM:SS")
        
        parser.add_argument("--hevc", action="store_true")
        parser.add_argument("--10-bit", dest="ten_bit", action="store_true")

        parser.add_argument("--target-bitrate", metavar="big|small")
        parser.add_argument("--hw-accel", action="store_true")
        parser.add_argument("--two-pass", action="store_true")
        parser.add_argument("--hrd", action="store_true")

        parser.add_argument("--crop", metavar="TOP:BOTTOM:LEFT:RIGHT")
        parser.add_argument("--no-crop", action="store_true")
        parser.add_argument("--deinterlace", action="store_true")
        parser.add_argument("--no-deinterlace", action="store_true")
        parser.add_argument("--preserve-field-rate", action="store_true")
        parser.add_argument("--par", metavar="X:Y")

        parser.add_argument("--audio", metavar="TRACK[ TRACK...]|LANGUAGE[ LANGUAGE...]|all", nargs="+")
        parser.add_argument("--stereo", action="store_true")

        parser.add_argument("--burn", metavar="TRACK", type=int)
        parser.add_argument("--no-burn", action="store_true")
        parser.add_argument("--subtitles", metavar="TRACK[ TRACK...]|LANGUAGE[ LANGUAGE...]|all", nargs="+")
        parser.add_argument("--no-subtitles", action="store_true")

        parser.add_argument("--skip-remux", action="store_true")
        parser.add_argument("-d", "--debug", action="store_true")
        parser.add_argument("-h", "--help", action="store_true")
        parser.add_argument("--version", action="store_true")

        args = parser.parse_args()

        self.debug = args.debug
        self.skip_remux = args.skip_remux
        self.dry_run = args.dry_run

        if args.version:
            print(version)
            exit()

        if args.help:
            print(help)
            exit()

        self.verify_tools()
        self.validate_args(args)

        output_file = os.path.splitext(basename(args.file))[0] + ".mkv"
        if not self.dry_run and os.path.exists(output_file):
            exit(f"Output file exists: {output_file}")

        media_info = self.scan_media(args.file)
        self.transcode(media_info, output_file)


    def validate_args(self, args):
        if not args.file:
            exit(f"Missing argument: file. Try `{basename(__file__)} --help` for more information")

        if not os.path.exists(args.file):
            exit(f"Input doesn't exist: {args.file}")
        
        if os.path.isdir(args.file):
            exit(f"Input cannot be a directory: {args.file}")

        def get_time_in_seconds(timestamp):
            pattern = re.compile("([0-9]{1,2}):([0-9]{1,2}):([0-9]{2})")
            match = pattern.match(timestamp)
            if match:
                hours, minutes, seconds = map(lambda x: int(x), match.groups())
                time = hours * 60 * 60
                time += minutes * 60
                time += seconds
                return time
            else:
                return None

        if args.start:
            start_time = get_time_in_seconds(args.start)
            if start_time:
                self.start_time = start_time
            else:
                exit(f"Invalid start: {args.start}")

        if args.stop:
            stop_time = get_time_in_seconds(args.stop)
            if stop_time:
                start_time = self.start_time if self.start_time else 0
                stop_time = stop_time - start_time
                if stop_time < 1:
                    exit("Stop time can't be before start")

                self.stop_time = stop_time
            else:
                exit(f"Invalid stop: {args.stop}")

        if args.target_bitrate:
            if args.target_bitrate == "big":
                self.video_bitrate_index += 1
            elif args.target_bitrate == "small":
                self.video_bitrate_index -= 1
            else:
                exit(f"Invalid bitrate target: {args.bitrate_target}")
            
        if args.hevc:
            self.video_bitrate_index -= 1
        
        if args.two_pass and args.hw_accel:
            exit("2-pass encoding is not supported by hardware encoders")
        
        self.two_pass = args.two_pass

        if args.ten_bit:
            for key in self.supported_encoders:
                self.supported_encoders[key]["name"] += "_10bit"

        format = "avc" if not args.hevc else "hevc"
        type = "sw" if not args.hw_accel else "hw"
        for key in self.supported_encoders:
            encoder = self.supported_encoders[key]
            if (encoder["name"] in self.available_video_encoders
                    and encoder["format"] == format
                    and encoder["type"] == type):
               self.encoder = encoder

        if not self.encoder:
            exit("No suitable video encoder found for requested settings")

        if args.hrd:
            if "x264" in self.encoder["name"]:
                self.encoder["encopts"] = "nal-hrd=vbr"
                self.encoder["maxrate"] = 1.5
                self.encoder["bufsize"] = 2
            elif "x265" in self.encoder["name"]:
                self.encoder["encopts"] += ":hrd=1"
            elif "vce" in self.encoder["name"]:
                self.encoder["encopts"] = "enforce_hrd=1"
            else:
                exit("No suitable encoder found for requested settings")

        if args.crop:
            if re.match("[0-9]+:[0-9]+:[0-9]+:[0-9]+", args.crop):
                self.crop = args.crop
            else:
                exit(f"Invalid crop geometry: {args.crop}")

        if args.no_crop:
            self.crop = None

        if args.deinterlace:
            self.deinterlace = True
        
        if args.no_deinterlace:
            self.deinterlace = False

        self.preserve_field_rate = args.preserve_field_rate

        if args.par:
            if re.match("[0-9]+:[0-9]+", args.par):
                self.par = args.par
            else:
                exit(f"Invalid aspect ratio: {args.par}")
        
        self.stereo = args.stereo
        
        if args.audio:
            self.audio = []
            for track in args.audio:
                if re.match("[0-9]+|[a-z]{3}", track):
                    self.audio.append(track)
                else:
                    exit(f"Invalid audio track selector: {track}")

        if args.burn:
            self.burned_sub = args.burn

        if args.no_burn:
            self.burned_sub = None

        if args.subtitles:
            self.subtitles = []
            for track in args.subtitles:
                if re.match("[0-9]+|[a-z]{3}", track):
                    self.subtitles.append(track)
                else:
                    exit(f"Invalid subtitle track selector: {track}")

        if args.no_subtitles:
            self.subtitles = []


    def transcode(self, media_info, output_file):
        input_file = media_info["filename"]
        command = ["HandBrakeCLI", "--input", input_file, "--output", output_file, "--markers"]
        command += (["--start-at", f"seconds:{self.start_time}"] if self.start_time else [])
        command += (["--stop-at", f"seconds:{self.stop_time}"] if self.stop_time else [])
        command += self.get_video_args(media_info)
        audio_args, audio_language = self.get_audio_args(media_info)
        command += audio_args
        subtitle_args, added_subs = self.get_subtitle_args(media_info, audio_language)
        command += subtitle_args

        print(" ".join(map(lambda x: shlex.quote(x), command)))
        if self.dry_run:
            exit()
        
        print("Transcoding...")
        log_file = open(f"{output_file}.log", "ab")
        transcode_success = self.run_command(command, log_file, capture_stdout=False)

        print("Postprocessing...")
        if added_subs:
            command = ["mkvpropedit", output_file]
            for index in range(len(added_subs)):
                command += ["--edit", f"track:s{index+1}", "--set", "flag-default=0"]
        
            self.run_command(command, log_file)

        if not self.skip_remux and os.path.exists(output_file):
            tmp_file = "tmp.mkv"
            i = 1
            while os.path.exists(tmp_file):
                tmp_file = f"tmp-{i}.mkv"
                i += 1
            
            os.rename(output_file, tmp_file)
            self.run_command(["mkvmerge", "-o", output_file, tmp_file], log_file)
            os.remove(tmp_file)

        log_file.close()

        if not transcode_success:
            exit("Transcode failed.")


    def get_video_args(self, media_info):
        args = []

        if self.par:
            args += ["--pixel-aspect", self.par]
        
        if self.crop:
            args += ["--crop", (media_info["video"]["detected_crop"] if self.crop == "auto" else self.crop)]
        else:
            args += ["--crop", "0:0:0:0"]

        framerate = media_info["video"]["fps"]
        interlacing_args = []
        if self.deinterlace == True or media_info["video"]["stored_interlaced"]:
            if self.preserve_field_rate:
                interlacing_args = ["--deinterlace=bob"]
                framerate = framerate * 2
            else:
                interlacing_args = ["--comb-detect", "--decomb"]

        args += interlacing_args

        hfr = framerate > 30
        bitrate_multiplier = 1 if not hfr else 1.2
        if media_info["video"]["width"] > 1280 or media_info["video"]["height"] > 720:
            target_bitrate = self.video_bitrate_ladder["1080p"][self.video_bitrate_index] * bitrate_multiplier
        elif media_info["video"]["width"] * media_info["video"]["height"] > 720 * 576:
            target_bitrate = self.video_bitrate_ladder["720p"][self.video_bitrate_index] * bitrate_multiplier
        else:
            target_bitrate = self.video_bitrate_ladder["sd"][self.video_bitrate_index] * bitrate_multiplier

        target_bitrate = int(target_bitrate)

        args += ["--encoder", self.encoder["name"], "--vb", str(target_bitrate)]

        encopts = ""
        if "encopts" in self.encoder:
            encopts += self.encoder["encopts"]

        if "maxrate" in self.encoder:
            if encopts:
                encopts += ":"

            encopts += f"vbv-maxrate={int(self.encoder['maxrate'] * target_bitrate)}"

        if "bufsize" in self.encoder:
            if encopts:
                encopts += ":"

            encopts += f"vbv-bufsize={int(self.encoder['bufsize'] * target_bitrate)}"

        if encopts:
            args += ["--encopts", encopts]

        if self.two_pass:
            args += ["--two-pass", "--turbo"]
        
        return args


    def get_audio_args(self, media_info):
        if "ca_aac" in self.available_audio_encoders:
            aac_encoder = "ca_aac"
        elif "fdk_aac" in self.available_audio_encoders:
            aac_encoder = "fdk_aac"
        elif "av_aac" in self.available_audio_encoders:
            aac_encoder = "av_aac"
        else:
            exit("No AAC audio encoder found")

        if "ac3" not in self.available_audio_encoders:
            exit("No AC3 audio encoder found")

        audio_bitrates = {"surround": 640, "stereo": 192, "mono": 96}

        selected_tracks = []
        for track in self.audio:
            if re.match("[0-9]+", track):
                selected_tracks.append(int(track))
            elif track == "all":
                selected_tracks = [t["index"] for t in media_info["audio"]]
                break
            else:
                selected_tracks += [t["index"] for t in media_info["audio"] if t["language"] == track]

        selected_tracks = reduce(lambda x,y: x+[y] if not y in x else x, selected_tracks, [])

        args = ["--audio", ",".join(map(lambda x: str(x), selected_tracks))]

        passthrough = ["ac3", "aac"]
        encoders = []
        mixdowns = []
        bitrates = []

        for track in selected_tracks:
            if track > len(media_info["audio"]):
                exit(f"Invalid track index: {track}")
            source_channels = media_info["audio"][track-1]["channels"]
            source_codec = media_info["audio"][track-1]["codec_name"]
            source_bitrate = media_info["audio"][track-1]["bit_rate"]
            source_bitrate = int(source_bitrate) / 1000 if source_bitrate != "unknown" else sys.maxsize

            if self.stereo:
                if source_channels <= 2 and source_codec in passthrough and source_bitrate <= audio_bitrates["stereo"] * 1.5:
                    encoders.append("copy")
                    mixdowns.append("")
                    bitrates.append("")
                else:
                    if source_channels > 2:
                        mixdowns.append("stereo")
                    else:
                        mixdowns.append("")
                    
                    encoders.append(aac_encoder)
                    bitrates.append(str(audio_bitrates["stereo"] if source_channels >= 2 else audio_bitrates["mono"]))
            else:
                if source_channels > 2:
                    key = "surround"
                    multiplier = 1
                elif source_channels == 2:
                    key = "stereo"
                    multiplier = 1.5
                else:
                    key = "mono"
                    multiplier = 1.5

                if source_codec in passthrough and source_bitrate <= audio_bitrates[key] * multiplier:
                    encoders.append("copy")
                    mixdowns.append("")
                    bitrates.append("")
                else:
                    if source_channels > 2:
                        encoders.append("ac3")
                    else:
                        encoders.append(aac_encoder)
                    mixdowns.append("")
                    bitrates.append(str(audio_bitrates[key]))

        args += ["--aencoder", ",".join(encoders)]
        for mixdown in mixdowns:
            if mixdown:
                args += ["--mixdown", ",".join(mixdowns)]
                break

        for bitrate in bitrates:
            if bitrate:
                args += ["--ab", ",".join(bitrates)]
                break

        return args, media_info["audio"][selected_tracks[0]-1]["language"]


    def get_subtitle_args(self, media_info, audio_language):
        selected_tracks = []
        if self.subtitles == "auto":
            selected_tracks += [t["index"] for t in media_info["subtitles"] if t["language"] == audio_language]
        else:
            for track in self.subtitles:
                if re.match("[0-9]+", track):
                    selected_tracks.append(int(track))
                elif track == "all":
                    selected_tracks = [t["index"] for t in media_info["audio"]]
                    break
                else:
                    selected_tracks += [t["index"] for t in media_info["subtitles"] if t["language"] == track]

        burned_track = None
        if self.burned_sub == "auto":
            for sub in media_info["subtitles"]:
                if sub["forced"]:
                    if burned_track:
                        exit("Multiple forced subtitle tracks detected")
                    burned_track = sub["index"]
        elif self.burned_sub:
            burned_track = self.burned_sub

        if burned_track:
            selected_tracks.insert(0, burned_track)

        selected_tracks = reduce(lambda x,y: x+[y] if not y in x else x, selected_tracks, [])

        args = []
        if selected_tracks:
            args += ["--subtitle", ",".join(map(lambda x: str(x), selected_tracks))]

        if burned_track:
            args += ["--subtitle-burned"]
            selected_tracks.remove(burned_track)

        return args, selected_tracks


    def run_command(self, command, log_file, capture_stdout=True):
        log_file.write((" ".join(map(lambda x: shlex.quote(x), command)) + "\n\n").encode("utf-8"))

        stdout_redirect = None if not capture_stdout else PIPE
        stderr_redirect = STDOUT if capture_stdout else PIPE
        with Popen(command, stdout=stdout_redirect, stderr=stderr_redirect) as p:
            for line in (p.stdout if capture_stdout else p.stderr):
                log_file.write(line)
                log_file.flush()
            
            try:
                p.wait()
                if p.returncode != 0:
                    message = f"Command failed: {command[0]}, exit code: {p.returncode}"
                    print(message)
                    log_file.write(f"{message}\n".encode("utf-8"))
                    return False
            except TimeoutExpired as e:
                log_file.write(f"Encoding failed: {e}\n".encode("utf-8"))
                exit(f"Encoding failed: {e}")
                return False
            
        return True

    def verify_tools(self):
        utils.verify_tools([
            ["ffprobe", "-version"],
            ["HandBrakeCLI", "--version"],
            ["mkvpropedit", "--version"],
            ["ffmpeg", "-version"],
            ["mkvmerge", "--version"]
        ])

        handbrake_help = run(["HandBrakeCLI", "--help"], stdout=PIPE, stderr=DEVNULL, universal_newlines=True).stdout

        video_encoders = []
        audio_encoders = []
        in_video_encoders_block = False
        in_audio_encoders_block = False
        for line in handbrake_help.splitlines():
            if "--encoder " in line:
                in_video_encoders_block = True
            elif "--aencoder " in line:
                in_audio_encoders_block = True
            elif (in_audio_encoders_block or in_video_encoders_block) and ("--" in line or "\"" in line):
                in_video_encoders_block = False
                in_audio_encoders_block = False
            elif in_video_encoders_block:
                video_encoders.append(line.strip())
            elif in_audio_encoders_block:
                audio_encoders.append(line.strip())

        if self.debug:
            print(video_encoders)
            print(audio_encoders)

        self.available_video_encoders = video_encoders
        self.available_audio_encoders = audio_encoders


    def scan_media(self, file):
        ffprobe_result = utils.scan_media(file)

        if self.debug:
            print(ffprobe_result)

        if self.crop and self.crop == "auto":
            detected_crop = utils.detect_crop(ffprobe_result)
        else:
            detected_crop = "unknown"

        filename = ffprobe_result["format"]["filename"]
        video = [{"width": s["width"], "height": s["height"], "stored_interlaced": s.get("field_order", "progressive") != "progressive", "detected_crop": detected_crop, "fps": float(Fraction(s["avg_frame_rate"]))} for s in ffprobe_result["streams"] if s["codec_type"] == "video"][0]
        audio = [{"channels": s["channels"], "codec_name": s["codec_name"], "bit_rate": s.get("bit_rate", "unknown"), "index": s["index"], "language": s.get("tags", {}).get("language", "und")} for s in ffprobe_result["streams"] if s["codec_type"] == "audio"]
        audio.sort(key=lambda a: a["index"])
        for i, a in enumerate(audio):
            a["index"] = i+1
        subtitles = [{"language": s.get("tags", {}).get("language", "und"), "forced": s["disposition"]["forced"], "index": s["index"]} for s in ffprobe_result["streams"] if s["codec_type"] == "subtitle"]
        subtitles.sort(key=lambda s: s["index"])
        for i, s in enumerate(subtitles):
            s["index"] = i+1

        media_info = {
            "filename": filename,
            "video": video,
            "audio": audio,
            "subtitles": subtitles
        }

        return media_info


def main():
    Transcoder().run()
