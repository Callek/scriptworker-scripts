#!/usr/bin/env python
"""Treescript l10n support."""
import logging
import os
import pprint
import tempfile

from scriptworker_client.aio import download_file, retry_async
from scriptworker_client.exceptions import DownloadError
from scriptworker_client.utils import load_json_or_yaml
from treescript.exceptions import TreeScriptError
from treescript.mercurial import run_hg_command
from treescript.task import (
    CLOSED_TREE_MSG,
    DONTBUILD_MSG,
    get_l10n_bump_info,
    get_dontbuild,
)

log = logging.getLogger(__name__)


# build_locale_map {{{1
def build_locale_map(old_contents, new_contents):
    """Build a map of changed locales for the commit message.

    Args:
        old_contents (dict): the old l10n changesets
        new_contents (dict): the bumped l10n changesets

    Returns:
        dict: the changes per locale

    """
    locale_map = {}
    for key in old_contents:
        if key not in new_contents:
            locale_map[key] = "removed"
    for k, v in new_contents.items():
        if old_contents.get(k, {}).get("revision") != v["revision"]:
            locale_map[k] = v["revision"]
        elif old_contents.get(k, {}).get("platforms") != v["platforms"]:
            locale_map[k] = v["platforms"]
    return locale_map


# build_platform_dict {{{1
def build_platform_dict(l10n_bump_info, repo_path):
    """Build a dictionary of locale to list of platforms.

    Args:
        l10n_bump_info (dict): the l10n_bump_info from the task payload.
        repo_path (str): the path to the repo on disk

    Returns:
        dict: the platform dict

    """
    platform_dict = {}
    ignore_config = l10n_bump_info.get("ignore_config", {})
    for platform_config in l10n_bump_info["platform_configs"]:
        path = os.path.join(repo_path, platform_config["path"])
        log.info("Reading %s for %s locales...", path, platform_config["platforms"])
        contents = load_json_or_yaml(path, is_path=True)
        for locale in contents.splitlines():
            # locale is 1st word in line in shipped-locales
            if platform_config.get("format") == "shipped-locales":
                locale = locale.split(" ")[0]
            existing_platforms = set(platform_dict.get(locale, {}).get("platforms", []))
            platforms = set(platform_config["platforms"])
            ignore_platforms = set(ignore_config.get(locale, []))
            platforms = (platforms | existing_platforms) - ignore_platforms
            platform_dict[locale] = {"platforms": sorted(list(platforms))}
    log.info("Built platform_dict:\n%s" % pprint.pformat(platform_dict))
    return platform_dict


# build_revision_dict {{{1
async def build_revision_dict(l10n_bump_info, version_list, repo_path):
    """Add l10n revision information to the ``platform_dict``.

    The l10n dashboard contains locale to revision information for each
    locale. If we have a ``revision_url``, that is the templatized dashboard
    url we should query for locale to revision information.

    Otherwise, add a ``default`` revision to each locale in the
    ``platform_dict``.

    Returns:
        dict: locale to dictionary of platforms and revision
    """
    log.info("Building revision dict...")
    platform_dict = build_platform_dict(l10n_bump_info, repo_path)
    revision_dict = {}
    if l10n_bump_info.get("revision_url"):
        repl_dict = {"MAJOR_VERSION": version_list[0]}

        url = l10n_bump_info["revision_url"] % repl_dict
        with tempfile.NamedTemporyFile() as fp:
            path = fp.name
            await retry_async(
                download_file, args=(url, path), retry_exceptions=(DownloadError,)
            )
            with open(path, "r") as fh:
                revision_info = fh.read()
        log.info("Got %s", revision_info)
        for line in revision_info.splitlines():
            locale, revision = line.split(" ")
            if locale in platform_dict:
                revision_dict[locale] = platform_dict[locale]
                revision_dict[locale]["revision"] = revision
    else:
        for k, v in platform_dict.items():
            v["revision"] = "default"
            revision_dict[k] = v
    log.info("revision_dict:\n%s" % pprint.pformat(revision_dict))
    return revision_dict


# build_commit_message {{{1
def build_commit_message(name, locale_map, dontbuild=False, ignore_closed_tree=False):
    """Build a commit message for the bumper.

    Args:
        name (str): the human readable name for the path (e.g. Firefox l10n
            changesets)
        locale_map (dict): l10n changeset changes, keyed by locale
        dontbuild (bool, optional): whether to add ``DONTBUILD`` to the
            comment. Defaults to ``False``
        ignore_closed_tree (bool, optional): whether to add ``CLOSED TREE``
            to the comment. Defaults to ``False``.

    Returns:
        str: the commit message

    """
    comments = ""
    approval_str = "r=release a=l10n-bump"
    for locale, revision in sorted(locale_map.items()):
        comments += "%s -> %s\n" % (locale, revision)
    if dontbuild:
        approval_str += DONTBUILD_MSG
    if ignore_closed_tree:
        approval_str += CLOSED_TREE_MSG
    message = "no bug - Bumping %s %s\n\n" % (name, approval_str)
    message += comments
    message = message.encode("utf-8")
    return message


# l10n_bump {{{1
async def l10n_bump(config, task, source_repo):
    """Perform a version bump.

    This function takes its inputs from task by using the ``get_l10n_bump_info``
    function from treescript.task. Using `next_version` and `files`.

    This function does nothing (but logs) if the current version and next version
    match, and nothing if the next_version is actually less than current_version.

    Args:
        config (dict): the running config
        task (dict): the running task
        source_repo (str): the source directory

    raises:
        TaskverificationError: if a file specified is not allowed, or
                               if the file is not in the target repository.

    """
    # check treestatus and task.payload.ignore_closed_tree
    l10n_bump_info = get_l10n_bump_info(task)
    # bump changesets and commit
