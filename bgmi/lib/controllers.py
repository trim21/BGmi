import os.path
import time
from operator import itemgetter
from typing import Any, Dict, List, Optional, Union

import filetype
import requests.exceptions

from bgmi.config import Source, cfg
from bgmi.lib.constants import BANGUMI_UPDATE_TIME, SUPPORT_WEBSITE
from bgmi.lib.download import Episode, download_prepare
from bgmi.lib.fetch import website
from bgmi.lib.models import (
    FOLLOWED_STATUS,
    STATUS_DELETED,
    STATUS_FOLLOWED,
    STATUS_NOT_DOWNLOAD,
    STATUS_UPDATED,
    Bangumi,
    DoesNotExist,
    Download,
    Filter,
    Followed,
    Subtitle,
    model_to_dict,
    recreate_source_relatively_table,
)
from bgmi.script import ScriptRunner
from bgmi.utils import (
    COLOR_END,
    GREEN,
    convert_cover_url_to_path,
    download_cover,
    episode_filter_regex,
    logger,
    normalize_path,
    print_error,
    print_info,
    print_success,
    print_warning,
)

ControllerResult = Dict[str, Any]


def add(name: str, episode: Optional[int] = None) -> ControllerResult:
    """
    ret.name :str
    """
    # action add
    # add bangumi by a list of bangumi name
    logger.debug("add name: %s episode: %d", name, episode)
    if not Bangumi.get_updating_bangumi():
        website.fetch(group_by_weekday=False)

    try:
        bangumi_obj = Bangumi.fuzzy_get(name=name)
    except Bangumi.DoesNotExist:
        result = {
            "status": "error",
            "message": f"{name} not found, please check the name",
        }
        return result
    followed_obj, this_obj_created = Followed.get_or_create(
        bangumi_name=bangumi_obj.name, defaults={"status": STATUS_FOLLOWED}
    )
    if not this_obj_created:
        if followed_obj.status == STATUS_FOLLOWED:
            result = {
                "status": "warning",
                "message": f"{bangumi_obj.name} already followed",
            }
            return result
        else:
            followed_obj.status = STATUS_FOLLOWED
            followed_obj.save()

    Filter.get_or_create(bangumi_name=name)

    max_episode, _ = website.get_maximum_episode(bangumi_obj, max_page=cfg.max_path)
    followed_obj.episode = max_episode if episode is None else episode

    followed_obj.save()
    result = {
        "status": "success",
        "message": f"{bangumi_obj.name} has been followed",
    }
    logger.debug(result)
    return result


def filter_(
    name: str,
    subtitle: Optional[str] = None,
    include: Optional[str] = None,
    exclude: Optional[str] = None,
    regex: Optional[str] = None,
) -> ControllerResult:
    result = {"status": "success", "message": ""}  # type: Dict[str, Any]
    try:
        bangumi_obj = Bangumi.fuzzy_get(name=name)
    except Bangumi.DoesNotExist:
        result["status"] = "error"
        result["message"] = f"Bangumi {name} does not exist."
        return result

    try:
        Followed.get(bangumi_name=bangumi_obj.name)
    except Followed.DoesNotExist:
        result["status"] = "error"
        result["message"] = "Bangumi {name} has not subscribed, try 'bgmi add \"{name}\"'.".format(
            name=bangumi_obj.name
        )
        return result

    followed_filter_obj, is_this_obj_created = Filter.get_or_create(bangumi_name=bangumi_obj.name)

    if is_this_obj_created:
        followed_filter_obj.save()

    if subtitle is not None:
        _subtitle = [s.strip() for s in subtitle.split(",")]
        _subtitle = [s["id"] for s in Subtitle.get_subtitle_by_name(_subtitle)]
        subtitle_list = [s.split(".")[0] for s in bangumi_obj.subtitle_group.split(", ") if "." in s]
        subtitle_list.extend(bangumi_obj.subtitle_group.split(", "))
        followed_filter_obj.subtitle = ", ".join(filter(lambda s: s in subtitle_list, _subtitle))

    if include is not None:
        followed_filter_obj.include = include

    if exclude is not None:
        followed_filter_obj.exclude = exclude

    if regex is not None:
        followed_filter_obj.regex = regex

    followed_filter_obj.save()
    subtitle_list = [s["name"] for s in Subtitle.get_subtitle_by_id(bangumi_obj.subtitle_group.split(", "))]

    result["data"] = {
        "name": bangumi_obj.name,
        "subtitle_group": subtitle_list,
        "followed": [s["name"] for s in Subtitle.get_subtitle_by_id(followed_filter_obj.subtitle.split(", "))]
        if followed_filter_obj.subtitle
        else [],
        "include": followed_filter_obj.include,
        "exclude": followed_filter_obj.exclude,
        "regex": followed_filter_obj.regex,
    }
    logger.debug(result)
    return result


def delete(name: str = "", clear_all: bool = False, batch: bool = False) -> ControllerResult:
    """
    :param name:
    :param clear_all:
    :param batch:
    """
    # action delete
    # just delete subscribed bangumi or clear all the subscribed bangumi
    result = {}
    logger.debug("delete %s", name)
    if clear_all:
        if Followed.delete_followed(batch=batch):
            result["status"] = "warning"
            result["message"] = "all subscriptions have been deleted"
        else:
            print_error("user canceled")
    elif name:
        try:
            followed = Followed.get(bangumi_name=name)
            followed.status = STATUS_DELETED
            followed.save()
            result["status"] = "warning"
            result["message"] = f"Bangumi {name} has been deleted"
        except Followed.DoesNotExist:
            result["status"] = "error"
            result["message"] = f"Bangumi {name} does not exist"
    else:
        result["status"] = "warning"
        result["message"] = "Nothing has been done."
    logger.debug(result)
    return result


def cal(force_update: bool = False, cover: Optional[List[str]] = None) -> Dict[str, List[Dict[str, Any]]]:
    logger.debug("cal force_update: {}", force_update)

    weekly_list = Bangumi.get_updating_bangumi()
    if not weekly_list:
        print_warning("Warning: no bangumi schedule, fetching ...")
        force_update = True

    if force_update:
        print_info("Fetching bangumi info ...")
        website.fetch()

    weekly_list = Bangumi.get_updating_bangumi()

    if cover is not None:
        # download cover to local
        cover_to_be_download = cover
        for daily_bangumi in weekly_list.values():
            for bangumi in daily_bangumi:
                _, file_path = convert_cover_url_to_path(bangumi["cover"])

                if not (os.path.exists(file_path) and filetype.is_image(file_path)):
                    cover_to_be_download.append(bangumi["cover"])

        if cover_to_be_download:
            print_info("Updating cover ...")
            download_cover(cover_to_be_download)

    runner = ScriptRunner()
    patch_list = runner.get_models_dict()
    for i in patch_list:
        weekly_list[i["update_time"].lower()].append(i)
    logger.debug(weekly_list)

    # for web api, return all subtitle group info
    r = weekly_list  # type: Dict[str, List[Dict[str, Any]]]
    for day, value in weekly_list.items():
        for index, bangumi in enumerate(value):
            bangumi["cover"] = normalize_path(bangumi["cover"])
            subtitle_group = [
                {"name": x["name"], "id": x["id"]}
                for x in Subtitle.get_subtitle_by_id(bangumi["subtitle_group"].split(", " ""))
            ]

            r[day][index]["subtitle_group"] = subtitle_group
    logger.debug(r)
    return r


def download(name: str, title: str, episode: int, download_url: str) -> None:
    download_prepare([Episode(name=name, title=title, episode=episode, download=download_url)])


def mark(name: str, episode: int) -> ControllerResult:
    """

    :param name: name of the bangumi you want to mark
    :param episode: bangumi episode you want to mark
    """
    result = {}
    try:
        followed_obj = Followed.get(bangumi_name=name)
    except Followed.DoesNotExist:
        runner = ScriptRunner()
        followed_obj = runner.get_model(name)  # type: ignore
        if not followed_obj:
            result["status"] = "error"
            result["message"] = f"Subscribe or Script <{name}> does not exist."
            return result

    if episode is not None:
        followed_obj.episode = episode
        followed_obj.save()
        result["status"] = "success"
        result["message"] = f"{name} has been mark as episode: {episode}"
    else:  # episode is None
        result["status"] = "info"
        result["message"] = f"{name}, episode: {followed_obj.episode}"
    return result


def search(
    keyword: str,
    count: Union[str, int] = cfg.max_path,
    regex: Optional[str] = None,
    dupe: bool = False,
    min_episode: Optional[int] = None,
    max_episode: Optional[int] = None,
    tag: bool = False,
    subtitle: Optional[str] = None,
) -> ControllerResult:
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 3
    try:
        if tag:
            data = website.search_by_tag(keyword, subtitle=subtitle, count=count)
        else:
            data = website.search_by_keyword(keyword, count=count)
        data = episode_filter_regex(data, regex=regex)
        if min_episode is not None:
            data = [x for x in data if x.episode >= min_episode]
        if max_episode is not None:
            data = [x for x in data if x.episode <= max_episode]

        if not dupe:
            data = Episode.remove_duplicated_bangumi(data)
        data.sort(key=lambda x: x.episode)
        return {
            "status": "success",
            "message": "",
            "options": {
                "keyword": keyword,
                "count": count,
                "regex": regex,
                "dupe": dupe,
                "min_episode": min_episode,
                "max_episode": max_episode,
            },
            "data": data,
        }
    except Exception as e:
        if os.environ.get("DEBUG"):
            raise
        return {
            "status": "error",
            "message": str(e),
            "options": {
                "keyword": keyword,
                "count": count,
                "regex": regex,
                "dupe": dupe,
                "min_episode": min_episode,
                "max_episode": max_episode,
            },
            "data": [],
        }


def source(data_source: str) -> ControllerResult:
    result = {}
    if data_source in list(map(itemgetter("id"), SUPPORT_WEBSITE)):
        recreate_source_relatively_table()
        cfg.data_source = Source(data_source)
        cfg.save()
        print_success("data source switch succeeds")
        result["status"] = "success"
        result["message"] = f"you have successfully change your data source to {data_source}"
    else:
        result["status"] = "error"
        result["message"] = "please check your input. data source should be one of {}".format(
            [x["id"] for x in SUPPORT_WEBSITE]
        )
    return result


def update(names: List[str], download: Optional[bool] = False, not_ignore: bool = False) -> ControllerResult:
    logger.debug("updating bangumi info with args: download: %r", download)
    downloaded: List[Episode] = []
    result: Dict[str, Any] = {
        "status": "info",
        "message": "",
        "data": {"updated": [], "downloaded": downloaded},
    }

    ignore = not bool(not_ignore)
    print_info("marking bangumi status ...")
    now = int(time.time())

    for follow in Followed.get_all_followed():
        if follow["updated_time"] and int(follow["updated_time"] + 60 * 60 * 24) < now:
            followed_obj = Followed.get(bangumi_name=follow["bangumi_name"])
            followed_obj.status = STATUS_FOLLOWED
            followed_obj.save()

    for script in ScriptRunner().scripts:
        obj = script.Model().obj
        if obj.updated_time and int(obj.updated_time + 60 * 60 * 24) < now:
            obj.status = STATUS_FOLLOWED
            obj.save()

    print_info("updating subscriptions ...")

    if not names:
        updated_bangumi_obj = Followed.get_all_followed()
    else:
        updated_bangumi_obj = []
        for n in names:
            try:
                f = Followed.get(bangumi_name=n)
                f = model_to_dict(f)
                updated_bangumi_obj.append(f)
            except DoesNotExist:
                pass

    runner = ScriptRunner()
    script_download_queue = runner.run()
    if script_download_queue and download:
        download_prepare(script_download_queue)
        downloaded.extend(script_download_queue)
        print_info("downloading ...")
        download_prepare(
            [
                Episode(**{key: value for key, value in x.items() if key not in ["id", "status"]})
                for x in Download.get_all_downloads(status=STATUS_NOT_DOWNLOAD)
            ]
        )

    for subscribe in updated_bangumi_obj:
        download_queue = []
        print_info(f"fetching {subscribe['bangumi_name']} ...")
        try:
            bangumi_obj = Bangumi.get(name=subscribe["bangumi_name"])
        except Bangumi.DoesNotExist:
            logger.error("Bangumi<{}> does not exists.", subscribe["bangumi_name"])
            continue
        try:
            followed_obj = Followed.get(bangumi_name=subscribe["bangumi_name"])
        except Followed.DoesNotExist:
            logger.error("Bangumi<{}> is not followed.", subscribe["bangumi_name"])
            continue

        try:
            episode, all_episode_data = website.get_maximum_episode(
                bangumi=bangumi_obj, ignore_old_row=ignore, max_page=cfg.max_path
            )
        except requests.exceptions.ConnectionError as e:
            print_warning(f"error {e} to fetch {bangumi_obj.name}, skip")
            continue

        saved_episode = subscribe.get("episode") or 0
        if episode > saved_episode:
            episode_range = range(saved_episode, episode + 1)
            print_success(f"{subscribe['bangumi_name']} updated, episode: {episode:d}")
            followed_obj.episode = episode
            followed_obj.status = STATUS_UPDATED
            followed_obj.updated_time = int(time.time())
            followed_obj.save()
            result["data"]["updated"].append({"bangumi": subscribe["bangumi_name"], "episode": episode})

            for i in episode_range:
                for epi in all_episode_data:
                    if epi.episode == i:
                        download_queue.append(epi)
                        break

        if download:
            download_prepare(download_queue)
            downloaded.extend(download_queue)

    if downloaded:
        failed = [Episode.parse_obj(x) for x in Download.get_all_downloads(status=STATUS_NOT_DOWNLOAD)]
        if failed:
            print_info("try to re-downloading previous failed torrents ...")
            download_prepare(failed)

    return result


def status_(name: str, status: int = STATUS_DELETED) -> ControllerResult:
    result = {"status": "success", "message": ""}

    if (status not in FOLLOWED_STATUS) or (not status):
        result["status"] = "error"
        result["message"] = f"Invalid status: {status}"
        return result

    status = int(status)
    try:
        followed_obj = Followed.get(bangumi_name=name)
    except Followed.DoesNotExist:
        result["status"] = "error"
        result["message"] = f"Followed<{name}> does not exists"
        return result

    followed_obj.status = status
    followed_obj.save()
    result["message"] = f"Followed<{name}> has been marked as status {status}"
    return result


def list_() -> ControllerResult:
    result = {}
    weekday_order = BANGUMI_UPDATE_TIME
    followed_bangumi = website.followed_bangumi()

    script_bangumi = ScriptRunner().get_models_dict()

    if not followed_bangumi and not script_bangumi:
        result["status"] = "warning"
        result["message"] = "you have not subscribed any bangumi"
        return result

    for i in script_bangumi:
        i["subtitle_group"] = [{"name": "<BGmi Script>"}]
        followed_bangumi[i["update_time"].lower()].append(i)

    result["status"] = "info"
    result["message"] = ""
    for weekday in weekday_order:
        if followed_bangumi[weekday.lower()]:
            result["message"] += f"{GREEN}{weekday}. {COLOR_END}"
            for j, bangumi in enumerate(followed_bangumi[weekday.lower()]):
                if bangumi["status"] in (STATUS_UPDATED, STATUS_FOLLOWED) and "episode" in bangumi:
                    bangumi["name"] = f"{bangumi['name']}({bangumi['episode']:d})"
                if j > 0:
                    result["message"] += " " * 5
                f = [x["name"] for x in bangumi["subtitle_group"]]
                result["message"] += "{}: {}\n".format(bangumi["name"], ", ".join(f) if f else "<None>")

    return result
