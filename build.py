#!/usr/bin/env python3

import argparse
import logging
import os
import re
import shutil
import subprocess as sp
import warnings

import distro

from config import OUTPUT_BASE_DIR, SRC_DIR, ARCH, OS, COMMAND, Config, GCLIENT_CONFIG, ungoogled_chromium_version, \
    ungoogled_chromium_android_version, parse_gn_flags, filter_list_file, git_maybe_checkout
from config import create_logger, shell_expand_abs_path
from config import chromium_version

# Logging
logger = create_logger(level=logging.DEBUG)


def clean(config):
    """
    Clean output directory.
    """
    if os.path.exists(config.output_base_dir):
        if shell_expand_abs_path(config.output_base_dir) != shell_expand_abs_path(
                OUTPUT_BASE_DIR):
            reply = input(
                "WARNING: you are about to remove an output directory which is different from the default location. "
                "Are you sure you want ot remove {}? [y/n]: ".format(
                    os.path.abspath(config.output_base_dir)))
            if reply != 'y':
                return

        sp.check_call(['rm', '-rf', config.output_base_dir])


def init(config):
    # Setup depot tools
    cwd = 'depot_tools'
    print("Cloning depot_tools...")
    if os.path.exists(cwd):
        shutil.rmtree(cwd)
    git_maybe_checkout(
        'https://chromium.googlesource.com/chromium/tools/depot_tools.git',
        cwd)

    # Clone chromium src
    clone_cmd = ['git', 'clone']
    if config.shallow:
        clone_cmd += ['--depth', '1', '--no-tags']
    print("Checking out chromium src...")
    if os.path.exists(SRC_DIR):
        logging.warning("Init: src folder already exists! Removing %s.", os.path.abspath(SRC_DIR))
        shutil.rmtree(SRC_DIR)
    sp.check_call(clone_cmd + [
        'https://chromium.googlesource.com/chromium/src.git',
        '-b', chromium_version])


def set_revision(config):
    """
    Update chromium source to needed revision.
    """
    # Check current checked out version
    cwd = SRC_DIR

    # Get current revision
    rev = sp.check_output(['git', 'rev-parse', 'HEAD'], cwd=cwd, encoding='utf8').strip()
    if config.shallow:
        return rev

    # Check whether the repo is shallow
    shallow = sp.check_output(['git', 'rev-parse', '--is-shallow-repository'], cwd=cwd, encoding='utf8').strip()
    if shallow == 'true':
        # Fail on shallow repo
        raise RuntimeError("Cannot set revision on a shallow repository!")

    # Do not catch git exception here because any error shall stop further steps
    tag = sp.run(['git', 'describe', '--tags', '--exact-match', rev], cwd=cwd, encoding='utf8', capture_output=True)
    if tag.returncode != 0 or tag.stdout.strip() != chromium_version:
        msg = "Current chromium commit is at " + rev + "(\x1B[3mtag: "
        if tag.returncode == 0:
            msg += tag.stdout
        msg += "\x1B[0m)."
        logging.info(msg + ', updating to \x1B[3mtag: ' + chromium_version + '\x1B[0m.')
        if config.reset:
            sp.check_call(['git', 'clean', '-fxd'], cwd=cwd)
            sp.check_call(['git', 'reset', '--hard'], cwd=cwd)
        sp.check_call(['git', 'pull'], cwd=cwd)
        sp.check_call(['git', 'checkout', chromium_version], cwd=cwd)
    else:
        logging.info("Current chromium commit is at " + chromium_version + ', no need to update.')

    new_rev = sp.check_output(['git', 'rev-parse', 'HEAD'], cwd=cwd, encoding='utf8').strip()
    return new_rev


def list_submodules():
    """
    List submodule names in current repo
    """
    submodule_names = []
    stages = sp.check_output(['git', 'ls-files', '--stage'], encoding='utf8').strip()
    submodules_list = re.findall(r"^160000", stages, flags=re.MULTILINE)
    logging.debug("Found submodules: " + '\n'.join(submodules_list))
    for submodule in submodules_list:
        # this assumes no spaces in submodule paths
        submodule_names.append(re.split(r"[ ]+", submodule.strip())[-1])
    return submodule_names


def update_submodules(hard_reset=False):
    """
    Update submodules.
    """
    # update HEAD
    sp.check_call(['git', 'pull'])

    submodules = list_submodules()

    # probe .git exists in submodules
    for submodule in submodules:
        logging.info(submodule + ' is at commit ',
                     sp.check_call(['git', 'rev-parse', 'HEAD'],
                                   cwd=submodule))

    # update all submodules
    print('Update submodules..')
    if hard_reset:
        for submodule in submodules:
            sp.check_call(['git', 'reset', '--hard'],
                          cwd=submodule)
    sp.check_call(['git', 'submodule', 'update', '--init', '--recursive'])


def sync(config):
    """
    Sync chromium source and run hooks.
    """
    # Fetch & Sync Chromium
    # Copy PATH from current process and add depot_tools to it
    depot_tools_path = os.path.join(os.getcwd(), 'depot_tools')
    if not os.path.exists(depot_tools_path) or not os.path.isdir(depot_tools_path):
        raise FileNotFoundError("Cannot find depot_tools!")
    _env = os.environ.copy()
    _env["PATH"] = depot_tools_path + ":" + _env["PATH"]

    # Get chromium ref
    # Set src HEAD to version
    chromium_ref = set_revision(config)

    # Create .gclient file
    with open('.gclient', 'w', encoding='utf-8') as f:
        f.write(GCLIENT_CONFIG.replace("@@TARGET_OS@@", "'{}'".format(config.target_os)))

    # Run gclient sync without hooks
    extra_args = []
    if config.reset:
        extra_args += ['--revision', 'src@' + chromium_ref, '--force', '--upstream', '--reset']
    if config.shallow:
        # There is a bug with --no-history when syncing third_party/wayland. See
        # https://bugs.chromium.org/p/chromium/issues/detail?id=1226496
        extra_args += ['--shallow']
    else:
        extra_args += ['--with_tags', '--with_branch_heads']

    sp.check_call(['gclient', 'sync', '--nohooks'] + extra_args, env=_env)

    # Run hooks
    sp.check_call(['gclient', 'runhooks'], env=_env)

    # If Debian/Ubuntu and install_deps, then run the script.
    # Note: requires sudo
    if config.install_build_deps:
        if config.target_os == 'android':
            script = 'install-build-deps-android.sh'
        else:
            script = 'install-build-deps.sh'
        distro_name = distro.linux_distribution(full_distribution_name=False)[0].lower()
        if distro_name == 'debian' or distro_name == 'ubuntu':
            warnings.warn("Note: installing dependencies requires root privilege!",
                          RuntimeWarning)
            sp.check_call(['sudo', os.path.join(SRC_DIR, 'build', script)])
        else:
            warnings.warn("Installing dependencies only works on Debian based systems, skipping.",
                          RuntimeWarning)


def prepare(config):
    """
    Pull ungoogled-chromium repositories, run scripts and apply patches.
    Note: for Android, this will use bundled SDK and NDK, not the rebuilds
    TODO: add a patch list filter
    """
    # Checkout ungoogled-chromium
    git_maybe_checkout(
        'https://github.com/Eloston/ungoogled-chromium.git',
        'ungoogled-chromium',
        branch=ungoogled_chromium_version, reset=True)
    if config.target_os == 'android':
        git_maybe_checkout(
            'https://github.com/ungoogled-software/ungoogled-chromium-android.git',
            'ungoogled-chromium-android',
            branch=ungoogled_chromium_android_version, reset=True)
        sp.check_call(['patch', '-p1', '--ignore-whitespace', '-i',
                       os.path.join('ungoogled-chromium-android', 'patches', 'Other', 'ungoogled-main-repo-fix.patch'),
                       '--no-backup-if-mismatch'])

    domain_substitution_cache_file = "domsubcache.tar.gz"
    if os.path.exists(domain_substitution_cache_file):
        os.remove(domain_substitution_cache_file)

    # ungoogled-chromium scripts
    # Do not check here because prune script return non-zero for non-existing files
    cwd = SRC_DIR
    uc_dir = 'ungoogled-chromium'
    utils_dir = os.path.join(uc_dir, 'utils')
    sp.run([os.path.join(utils_dir, 'prune_binaries.py'),
        SRC_DIR, filter_list_file(
            uc_dir, 'pruning.list',
            excludes=['buildtools/linux64/gn'])])
    sp.check_call([os.path.join(utils_dir, 'patches.py'),
        'apply', 'src', os.path.join(uc_dir, 'patches')])
    sp.check_call([os.path.join(utils_dir, 'domain_substitution.py'),
        'apply', '-r', os.path.join(uc_dir, 'domain_regex.list'),
        '-f', filter_list_file(uc_dir, 'domain_substitution.list'),
        '-c', domain_substitution_cache_file, 'src'])

    # ungoogled-chromium-android scripts
    if config.target_os == 'android':
        if os.path.exists(domain_substitution_cache_file):
            os.remove(domain_substitution_cache_file)

        uca_dir = 'ungoogled-chromium-android'
        sp.run([os.path.join(utils_dir, 'prune_binaries.py'),
            'src', filter_list_file(uca_dir, 'pruning_2.list')])
        sp.check_call([os.path.join(utils_dir, 'patches.py'),
            'apply', 'src', os.path.join(uca_dir, 'patches')])
        sp.check_call([os.path.join(utils_dir, 'domain_substitution.py'),
            'apply', '-r', os.path.join(uc_dir, 'domain_regex.list'),
            '-f', filter_list_file(uca_dir, 'domain_sub_2.list'),
            '-c', domain_substitution_cache_file, 'src'])


def build(config):
    """
    Run build for given targets.
    """
    # Create output folder if not exist
    release_channel = 'Release' if not config.debug else 'Debug'
    output_subfolder = release_channel + '_' + config.target_os + '_' + config.target_cpu
    output_path = os.path.join(config.output_base_dir, output_subfolder)
    output_src_path = os.path.join(SRC_DIR, config.output_base_dir, output_subfolder)
    if os.path.exists(output_src_path):
        if not os.path.isdir(output_src_path):
            os.remove(output_src_path)
    os.makedirs(output_src_path, exist_ok=True)

    # Build GN args
    # ungoogled-chromium
    with open(os.path.join('ungoogled-chromium', 'flags.gn'), 'r') as f:
        flags = f.readlines()

    gn_args = parse_gn_flags(flags)

    # Extra flags
    # Common flags
    gn_args.update({
        'is_component_build': 'false',
        'is_unsafe_developer_build': 'false',
        'proprietary_codecs': 'true',
        'ffmpeg_branding': '"Chrome"',
        'use_gnome_keyring': 'false',
        'exclude_unwind_tables': 'false',
        'target_os': '"' + config.target_os + '"',
        'target_cpu': '"' + config.target_cpu + '"',
    })

    # Debug flags
    if config.debug:
        gn_args.update({
            'is_debug': 'true',
            'is_unsafe_developer_build': 'true',
            'is_official_build': 'false',
            'symbol_level': '1',
            'blink_symbol_level': '1',
        })
    else:
        gn_args.update({
            'is_debug': 'false',
            'is_unsafe_developer_build': 'false',
            'is_official_build': 'true',
            'symbol_level': '0',
            'blink_symbol_level': '0',
        })

    # CC Wrapper
    if config.cc_wrapper is not None:
        gn_args.update({
            'cc_wrapper': '"' + config.cc_wrapper + '"',
        })

    # Command line override
    gn_args.update(config.gn_args)

    # Assemble args
    gn_args_str = ""
    delimiter = ' ' if config.direct_download else '\n'
    for k, v in gn_args.items():
        gn_args_str += '='.join([k, v]) + delimiter

    # Add depot_tools to env
    depot_tools_path = os.path.join(os.getcwd(), 'depot_tools')
    if not os.path.exists(depot_tools_path) or not os.path.isdir(depot_tools_path):
        raise FileNotFoundError("Cannot find depot_tools!")
    _env = os.environ.copy()
    _env["PATH"] = depot_tools_path + ":" + _env["PATH"]

    # Run GN
    if config.direct_download:
        sp.check_call([
            os.path.join(SRC_DIR, 'tools', 'gn', 'bootstrap', 'bootstrap.py'),
            "--gn-gen-args='" + gn_args_str + "'"])
    else:
        # Do not use --args. It requires all double quotes be escaped.
        with open(os.path.join(output_src_path, 'args.gn'), 'w', encoding='utf-8') as f:
            f.write(gn_args_str)
        sp.check_call(['gn', 'gen', output_path, '--fail-on-unused-args'], cwd=SRC_DIR, env=_env)

    # Run ninja
    if config.target_os == 'linux':
        targets = ['chrome', 'chrome_sandbox', 'chromedriver']
    elif config.target_os == 'android':
        targets = ['chrome_modern_public_bundle']
    else:
        raise AttributeError("Target OS not supported")
    sp.check_call(['autoninja', '-j', str(config.num_jobs), '-C', output_path,
        *targets], cwd=SRC_DIR, env=_env)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description='ungoogled-chromium build script',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        allow_abbrev=False)
    parser.add_argument('command',
                        type=str, choices=COMMAND,
                        help='Command to run, can be one of '
                             + '|'.join(COMMAND))

    parser.add_argument('-a', '--arch', type=str, default=ARCH[3], choices=ARCH,
                        help='arch can be one of ' + '|'.join(ARCH))
    parser.add_argument('-g', '--gn-args', type=str,
                        help='GN build arguments override in the format of key1=value1;key2=value2;')
    parser.add_argument('-o', '--output-dir', type=str, default=OUTPUT_BASE_DIR,
                        help='base path for build output relative to {}. Defaults to {}'.format(
                            SRC_DIR, OUTPUT_BASE_DIR))
    parser.add_argument('-s', '--os', type=str, default=OS[0], choices=OS,
                        help='OS can be one of: ' + '|'.join(OS))
    parser.add_argument('--cc_wrapper', type=str,
                        help='Set cc_wrapper for build.')
    parser.add_argument('--debug', action='store_true',
                        help='Build debug builds')
    parser.add_argument('--install-build-deps', action='store_true',
                        help="Run chromium's install-build-deps(-android).sh during sync")

    group = parser.add_mutually_exclusive_group()
    group.add_argument('--direct-download', action='store_true',
                       help='Use source from https://commondatastorage.googleapis.com/chromium-browser-official')
    group.add_argument('--shallow', action='store_true',
                       help='Do not clone git history for chromium source')

    parser.add_argument('--reset', action='store_true',
                       help='Reset chromium source for sync')

    args = parser.parse_args()
    logger.debug('args: %s', args)

    config = Config(args)
    logger.debug('config: %s', config)

    if args.command == 'init':
        init(config)
    elif args.command == 'sync':
        sync(config)
    elif args.command == 'prepare':
        prepare(config)
    elif args.command == 'build':
        build(config)
    elif args.command == 'clean':
        clean(config)
