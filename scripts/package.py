#!/usr/bin/env python3

# Produces the assets release candidates in 'dist'.
#
# The outputs depend of the host system.
#
#    For linux/ubuntu: ta-lib-<version>-src.tar.gz
#         with contents for doing "./configure; make; sudo make install"
#
#    For windows: ta-lib-<version>-windows-<platform>.zip
#         with contents:
#            bin/ta-lib.dll        (dynamic library)
#            lib/ta-lib.lib        (import library)
#            lib/ta-lib-static.lib (static library)
#            include/*.h           (API headers)
#            VERSION.txt           Version number "major.minor.patch"
#
# How to run it?
#   Do './scripts/package.py' while current directory is the root of the ta-lib repository.
#
#   Windows Specific:
#    - You must have Visual Studio installed (free community version works).
#    - Host machine *must* be x64.
#
#    (FYI, all this can optionally be done in a Windows VM)
#
# How to change the version?
#   Edit MAJOR, MINOR, PATCH in src/ta_common/ta_version.c
#   There is no need to modify other files (they will be updated by this script).
#
#   See README-DEVS.md for all the release steps.

import argparse
import filecmp
from multiprocessing.context import _force_start_method
import os
import shlex
import subprocess
import sys
import glob
import platform
import shutil
import tempfile
import zipfile
import zlib

from utilities.windows import call_vcvarsall
from utilities.versions import sync_sources_digest, sync_versions
from utilities.common import are_generated_files_git_changed, compare_dir, copy_file_list, create_temp_dir, get_src_generated_files, is_arm64_toolchain_installed, is_cmake_installed, is_debian_based, is_dotnet_installed, is_i386_toolchain_installed, is_redhat_based, is_rpmbuild_installed, is_ubuntu, is_dotnet_installed, is_wix_installed, is_x86_64_toolchain_installed, run_command, run_command_term, verify_git_repo, run_command_sudo
from utilities.files import compare_msi_files, compare_tar_gz_files, compare_zip_files, create_rtf_from_txt, create_zip_file, compare_deb_files, force_delete, force_delete_glob, path_join

def delete_other_versions(target_dir: str, file_pattern: str, new_version: str ):
    # Used for cleaning-up a directory from other versions than the one
    # being newly built.
    #
    # Example of file_pattern: 'ta-lib-*-src.tar.gz'
    glob_all_packages = path_join(target_dir, file_pattern)
    for file in glob.glob(glob_all_packages):
        if new_version not in file:
            force_delete(file)

def do_cmake_reconfigure(root_dir: str, options: str, sudo_pwd: str = "") ->str:
    # Clean-up any potential previous build, and configure a new build
    # using the CMakeLists.txt and specified options.
    #
    # Returns the "build" location where further cmake/cpack should be done from.
    #
    # Exit on any error.
    build_dir = path_join(root_dir, 'build')
    if os.path.exists(build_dir):
        force_delete(build_dir, sudo_pwd)

    if not is_cmake_installed():
        print("Error: CMake not found. You need to install it and be accessible with PATH.")
        sys.exit(1)

    cmake_lists = path_join(root_dir, 'CMakeLists.txt')
    if not os.path.isfile(cmake_lists):
        print(f"Error: {cmake_lists} not found. Your working directory must be within a cloned TA-Lib repos")
        sys.exit(1)

    # Delete ta_config.h to force regeneration.
    ta_config_h = path_join(root_dir, 'include', 'ta_config.h')
    force_delete(ta_config_h, sudo_pwd)

    # Run CMake configuration.
    original_dir = os.getcwd()
    try:
        os.makedirs(build_dir)
        os.chdir(build_dir)
        cmake_command = ['cmake'] + shlex.split(options) + ['..']
        formatted_command = ' '.join([f'[{elem}]' for elem in cmake_command])
        print("CMake configure command:", formatted_command)  # to help debugging, display each arg in brackets
        run_command_term(cmake_command) # Run the command in foreground (output displayed).
    finally:
        # Restore the original working directory
        os.chdir(original_dir)

    return build_dir

def do_cmake_build(build_dir: str):
    # Exit on any error.
    original_dir = os.getcwd()
    try:
        os.chdir(build_dir)
        cmake_command = ['cmake', '--build', '.']
        formatted_command = ' '.join([f'[{elem}]' for elem in cmake_command])
        print("CMake build command:", formatted_command)  # to help debugging, display each arg in brackets
        run_command(cmake_command) # Output piped and displayed only on error.
    finally:
        # Restore the original working directory
        os.chdir(original_dir)

def do_cpack_build(build_dir: str):
    # Exit on any error.
    original_dir = os.getcwd()
    try:
        os.chdir(build_dir)
        cpack_command = ['cpack', '.']
        formatted_command = ' '.join([f'[{elem}]' for elem in cpack_command])
        print("CPack command:", formatted_command)  # to help debugging, display each arg in brackets
        run_command_term(cpack_command)
    finally:
        # Restore the original working directory
        os.chdir(original_dir)

def find_asset_with_ext(target_dir, version: str, extension: str) -> str:
    # Useful for identifying the name of the file generated by CMake.
    # Exit on error.
    glob_deb = path_join(target_dir, f"*.{extension}")
    files = glob.glob(glob_deb)
    if len(files) != 1:
        print(f"Error: Expected one .{extension} file, found {len(files)}")
        sys.exit(1)

    # Check that the single file has the version as substring
    filepath = files[0]
    if version not in filepath:
        print(f"Error: Expected version {version} not found in {filepath}")
        sys.exit(1)

    return os.path.basename(filepath)

def package_windows_zip(root_dir: str, version: str, platform: str) -> dict:
    result: dict = {"success": False}

    file_name_prefix = f'ta-lib-{version}-windows-{platform}'
    asset_file_name = f'{file_name_prefix}.zip'
    result["asset_file_name"] = asset_file_name

    # Clean-up
    dist_dir = path_join(root_dir, 'dist')
    delete_other_versions(dist_dir,"ta-lib-*.zip",version)

    temp_dir = path_join(root_dir, 'temp')
    delete_other_versions(temp_dir,"ta-lib-*.zip",version)

    force_delete_glob(temp_dir, "ta-lib-*")

    # Build the libraries
    build_dir = do_cmake_reconfigure(root_dir, '-G Ninja -DBUILD_DEV_TOOLS=OFF -DCMAKE_BUILD_TYPE=Release')
    do_cmake_build(build_dir)

    # Create a temporary zip package to test before copying to dist.
    package_temp_dir = path_join(temp_dir, file_name_prefix)
    temp_lib_dir = path_join(package_temp_dir, 'lib')
    temp_bin_dir = path_join(package_temp_dir, 'bin')
    temp_include_dir = path_join(package_temp_dir, 'include')

    os.makedirs(temp_dir, exist_ok=True)
    os.makedirs(package_temp_dir, exist_ok=True)
    os.makedirs(temp_lib_dir, exist_ok=True)
    os.makedirs(temp_bin_dir, exist_ok=True)
    os.makedirs(temp_include_dir, exist_ok=True)

    # Copy the built files to the temporary package locations.
    original_dir = os.getcwd()
    include_rootdir = path_join(root_dir, 'include')
    try:
        os.chdir(build_dir)
        shutil.copy('ta-lib.dll', temp_bin_dir)
        shutil.copy('ta-lib.lib', temp_lib_dir)
        shutil.copy('ta-lib-static.lib', temp_lib_dir)
        for header_filepath in glob.glob(path_join(include_rootdir, '*.h')):
            if "ta_config.h" in header_filepath:
                # At this point, this build artifact header is no longuer needed.
                force_delete(header_filepath)
                continue
            shutil.copy(header_filepath, temp_include_dir)

        # Create the VERSION.txt file
        with open(path_join(package_temp_dir, 'VERSION.txt'), 'w') as f:
            f.write(version)
    except subprocess.CalledProcessError as e:
        print(f"Error copying files: {e}")
        return result
    finally:
        # Restore the original working directory
        os.chdir(original_dir)

    # Compress the package (.zip)
    package_temp_file = path_join(temp_dir, asset_file_name)
    try:
        create_zip_file(package_temp_dir, package_temp_file)
    except subprocess.CalledProcessError as e:
        print(f"Error creating zip file: {e}")
        return

    # TODO Add testing of the temporary package here.

    # Temporary zip is verified OK, so copy it into dist, but only if its content is different.
    os.makedirs(dist_dir, exist_ok=True)
    dist_file = path_join(dist_dir, asset_file_name)
    package_existed = os.path.exists(dist_file)
    package_copied = False
    if not package_existed or not compare_zip_files(package_temp_file, dist_file):
        os.makedirs(dist_dir, exist_ok=True)
        shutil.copy(package_temp_file, dist_file)
        package_copied = True

    result["success"] = True
    result["existed"] = package_existed
    result["copied"] = package_copied
    return result

def package_windows_msi(root_dir: str, version: str, platform: str, force: bool) -> dict:
    result: dict = {"success": False}

    # Clean-up
    dist_dir = path_join(root_dir, 'dist')
    delete_other_versions(dist_dir,"ta-lib-*.msi",version)

    temp_dir = path_join(root_dir, 'temp')
    force_delete(path_join(temp_dir, "PFiles"))

    force_delete_glob(temp_dir, "ta-lib-*")

    # MSI supports only .rtf for license, so generate it into root_dir.
    license_txt = path_join(root_dir,"LICENSE")
    license_rtf = path_join(root_dir,"LICENSE.rtf")
    create_rtf_from_txt(license_txt,license_rtf)

    if not is_dotnet_installed():
       print("Error: .NET Framework not found. It is required to build the MSI.")
       return result

    if not is_wix_installed():
        print("Error: WiX Toolset not found. It is required to build the MSI.")
        return result

    build_dir = do_cmake_reconfigure(root_dir, '-G Ninja -DCPACK_GENERATOR=WIX -DBUILD_DEV_TOOLS=OFF -DCMAKE_BUILD_TYPE=Release')
    do_cmake_build(build_dir) # Build the libraries
    do_cpack_build(build_dir) # Create the .msi

    asset_file_name = find_asset_with_ext(build_dir, version, "msi")
    temp_dist_file = path_join(build_dir, asset_file_name)

    dist_dir = path_join(root_dir, 'dist')
    dist_file = path_join(dist_dir, asset_file_name)
    package_existed = os.path.exists(dist_file)
    package_copied = False
    if force or not package_existed or not compare_msi_files(temp_dist_file, dist_file):
        os.makedirs(dist_dir, exist_ok=True)
        if os.path.exists(dist_file):
            os.remove(dist_file)
        os.rename(temp_dist_file, dist_file)
        package_copied = True

    result["success"] = True
    result["asset_file_name"] = asset_file_name
    result["existed"] = package_existed
    result["copied"] = package_copied
    return result

def package_deb(root_dir: str, version: str, sudo_pwd: str, toolchain: str, force_overwrite: bool) -> dict:
    # Create .deb packaging to be installed with apt or dpkg (Debian-based systems).
    #
    # TA-Lib will install under '/usr/lib' and '/usr/include/ta-lib'.
    #
    # The asset is created/updated into the 'dist' directory only when it
    # pass some tests.
    #
    result: dict = {"success": False}

    # Check dependencies.
    if not is_debian_based():
        print("Error: Debian-based system required for .deb packaging.")
        return result

    if not is_cmake_installed():
        print("Error: CMake not found. It is required to be install for .deb creation")
        return result

    cmakelists = os.path.join(root_dir, 'CMakeLists.txt')
    if not os.path.isfile(cmakelists):
        print(f"Error: {cmakelists} not found. Make sure you are working within a TA-Lib repos")
        return result

    # Clean-up
    dist_dir = path_join(root_dir, 'dist')
    delete_other_versions(dist_dir,"*.deb",version)

    # Build the libraries
    configure_options = '-DCPACK_GENERATOR=DEB -DBUILD_DEV_TOOLS=OFF'

    if toolchain:
        cmake_dir = path_join(root_dir, 'cmake')
        toolchain_file = path_join(cmake_dir, toolchain)
        configure_options += f' -DCMAKE_TOOLCHAIN_FILE={toolchain_file}'

    build_dir = do_cmake_reconfigure(root_dir, configure_options, sudo_pwd)
    do_cmake_build(build_dir)
    do_cpack_build(build_dir)

    # Get the asset file name (from the only .deb file expected in the build directory)
    glob_deb = os.path.join(build_dir, '*.deb')
    deb_files = glob.glob(glob_deb)
    if len(deb_files) != 1:
        print(f"Error: Expected one .deb file, found {len(deb_files)}")
        return result

    # Check that the single .deb file has the version as substring
    deb_file = deb_files[0]
    if version not in deb_file:
        print(f"Error: Expected version {version} in {deb_file}")
        return result

    asset_file_name = os.path.basename(deb_file)

    # Sanity check that asset_file_name is correct.
    test_file_name = os.path.join(build_dir, asset_file_name)
    if not os.path.exists(test_file_name):
        print(f"Error: {test_file_name} not found.")
        return result

    # TODO Add here some "end-user installation" testing.

    # Copy the .deb file into dist, but only if it is binary different
    # The creation date will be ignored.
    dist_file = os.path.join(dist_dir, asset_file_name)
    package_existed = os.path.exists(dist_file)
    package_copied = False
    if force_overwrite or not package_existed or not compare_deb_files(deb_file, dist_file):
        os.makedirs(dist_dir, exist_ok=True)
        os.rename(deb_file, dist_file)
        package_copied = True

    result["success"] = True
    result["asset_file_name"] = asset_file_name
    result["existed"] = package_existed
    result["copied"] = package_copied
    return result

def package_rpm(root_dir: str, version: str, sudo_pwd: str) -> dict:
    result: dict = {"success": False}
    # Not implemented yet
    return result

def package_src_tar_gz(root_dir: str, version: str, sudo_pwd: str) -> dict:
    # The src.tar.gz is for users wanting to build from source with autotools (./configure).
    #
    # The asset is created/updated into the 'dist' directory only when it
    # pass some tests.
    #
    result: dict = {"success": False}

    asset_file_name = f"ta-lib-{version}-src.tar.gz"
    result["asset_file_name"] = asset_file_name

    dist_dir = os.path.join(root_dir, 'dist')

    # Delete previous dist packaging
    glob_all_packages = os.path.join(dist_dir, '*-src.tar.gz')
    for file in glob.glob(glob_all_packages):
        if not file.endswith(asset_file_name):
            force_delete(file)

    temp_dir = os.path.join(root_dir, 'temp')
    package_temp_file_prefix = f"ta-lib-{version}"
    package_temp_dir = os.path.join(temp_dir, package_temp_file_prefix)
    package_temp_file_src = os.path.join(root_dir, f"{package_temp_file_prefix}.tar.gz")
    package_temp_file_dest = os.path.join(temp_dir, f"{package_temp_file_prefix}.tar.gz")

    force_delete(package_temp_file_src, sudo_pwd)
    force_delete(package_temp_file_dest, sudo_pwd)
    force_delete(package_temp_dir, sudo_pwd)

    # Always autoreconf before re-packaging
    try:
        subprocess.run(['autoreconf', '-fi'], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error running 'autoreconf -fi': {e}")
        return result

    # Run ./configure
    try:
        subprocess.run(['./configure'], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error running './configure': {e}")
        return result

    # Run make dist
    try:
        subprocess.run(['make', 'dist'], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error running 'make dist': {e}")
        return result

    # Move ta-lib-{version}.tar.gz into temp directory.
    if not os.path.isfile(package_temp_file_src):
        print(f"Error: {package_temp_file_src} not found.")
        return result

    os.makedirs(temp_dir, exist_ok=True)
    os.rename(package_temp_file_src, package_temp_file_dest)

    # From this point, simulate the "end-user" dev experience
    # as if the source package was freshly downloaded.
    os.chdir(temp_dir)
    os.system(f"tar -xzf {package_temp_file_dest}")
    if not test_autotool_src(package_temp_dir, sudo_pwd):
        print("Error: Source package verification failed.")
        return result

    # Move ta-lib-{version}.tar.gz into root_dir/dist (create directory as needed)
    # at same time rename it ta-lib-{version}-src.tar.gz
    # ...
    # but do this only if the archive *content* has changed
    # (ignore metadata such as file creation time).
    dist_file = os.path.join(dist_dir, asset_file_name)
    package_existed = os.path.exists(dist_file)
    package_copied = False
    if not package_existed or not compare_tar_gz_files(package_temp_file_dest, dist_file):
        os.makedirs(dist_dir, exist_ok=True)
        os.rename(package_temp_file_dest, dist_file)
        package_copied = True

    # Some clean-up (but keep the untarred directory for debugging)
    force_delete(package_temp_file_src, sudo_pwd)
    force_delete(package_temp_file_dest, sudo_pwd)

    result["success"] = True
    result["existed"] = package_existed
    result["copied"] = package_copied
    return result

def test_autotool_src(configure_dir: str, sudo_pwd: str) -> bool:
    # Returns True on success.

    # sudo_pwd is optional.
    #
    # configure_dir is the location where the end-user is expected to
    # do './configure'.
    #
    # For this test, configure_dir must be somwhere within the TA-Lib
    # git repos (will typically be under ta-lib/temp).
    #
    # In specified configure_dir do:
    # - './configure'
    # - 'make' (verify returning zero)
    # - Run './src/tools/ta_regtest/ta_regtest' (verify returning zero)
    # - Run './src/tools/gen_code/gen_code' (verify no unexpected changes)
    # - 'sudo make install' (verify returning zero)

    original_dir = os.getcwd()
    root_dir = verify_git_repo()
    generated_files_temp_copy_1 = create_temp_dir(root_dir)
    generated_files_temp_copy_2 = create_temp_dir(root_dir)
    os.chdir(configure_dir)

    try:
        git_changed = are_generated_files_git_changed(root_dir);
        copy_file_list(configure_dir,
                       generated_files_temp_copy_1,
                       get_src_generated_files())

        # Simulate typical user installation.
        subprocess.run(['./configure'], check=True)
        subprocess.run(['make'], check=True)

        # Run its src/tools/ta_regtest/ta_regtest
        subprocess.run(['src/tools/ta_regtest/ta_regtest'], check=True)

        if not os.path.isfile('src/tools/gen_code/gen_code'):
            print("Error: src/tools/gen_code/gen_code does not exist.")
            return False

        # Re-running gen_code should not cause changes to the root directory.
        # (but do nothing if there was already git changes prior to gen_code).
        # This is just a sanity check that the script is not breaking something
        # unexpected outside of the "end-user simulated" directory.
        if not git_changed and are_generated_files_git_changed(root_dir):
            print("Error: Unexpected changes from gen_code to root_dir. Do 'git diff'")
            return False

        # Now verify if gen_code did change files unexpectably within
        # the "end-user simulated" directory.
        #
        # It should not, because we are at the point of testing the src
        # package, which should have the latest generated files version
        # and re-running gen_code should have no effect.
        copy_file_list(configure_dir,
                       generated_files_temp_copy_2,
                       get_src_generated_files())
        if not compare_dir(generated_files_temp_copy_1, generated_files_temp_copy_2):
            return False

        run_command_sudo(['make', 'install'], sudo_pwd)

    except subprocess.CalledProcessError as e:
        print(f"Error during verification: {e}")
        return False

    finally:
        os.chdir(original_dir)

    return True

def display_package_results(results: dict):
    # Display the results returned by most packaging functions.
    asset_built = results.get("built", False)
    asset_built_success = results.get("success", False)
    asset_file_name = results.get("asset_file_name", "unknown")
    asset_copied = results.get("copied", False)
    asset_existed = results.get("existed", False)
    if asset_built:
        if not asset_built_success:
            print(f"Error: Packaging dist/{asset_file_name} failed.")

        if asset_copied:
            if asset_existed:
                print(f"Updated dist/{asset_file_name}")
            else:
                print(f"Created dist/{asset_file_name}")
        else:
            print(f"No changes for dist/{asset_file_name}")
    else:
        print(f"{asset_file_name} build skipped (not supported on this platform)")

def package_all_linux(root_dir: str, version: str, sudo_pwd: str):
    os.chdir(root_dir)

    # The dist/ta-lib-*-src.tar.gz are created only on Ubuntu
    # but are tested with other Linux distributions.
    src_tar_gz_results = {
        "success": False,
        "built": False,
        "asset_file_name": "src.tar.gz", # Default, will change.
    }

    # The .tar.gz file is better at detecting if the *content* is different.
    # If any changes are detected, it will force the creation and overwrite
    # of all the other packages.
    force_overwrite = False

    if is_ubuntu():
        results = package_src_tar_gz(root_dir, version, sudo_pwd)
        src_tar_gz_results.update(results)
        src_tar_gz_results["built"] = True
        if not src_tar_gz_results["success"]:
            print(f'Error: Packaging dist/{src_tar_gz_results["asset_file_name"]} failed.')
            sys.exit(1)
        if src_tar_gz_results["copied"]:
            force_overwrite = True

    # When supported by host, build RPM using CMakeLists.txt (CPack)
    rpm_results = {
        "success": False,
        "built": False,
        "asset_file_name": ".rpm", # Default, will change.
    }
    if is_redhat_based():
        if not is_rpmbuild_installed():
            print("Error: rpmbuild not found. RPM not created")
            sys.exit(1)
        results = package_rpm(root_dir, version, sudo_pwd)
        rpm_results.update(results)
        rpm_results["built"] = True
        if not rpm_results["success"]:
            print(f'Error: Packaging dist/{rpm_results["asset_file_name"]} failed.')
            sys.exit(1)

    # When supported by host, build DEB using CMakeLists.txt (CPack)
    deb_results_arm64 = {
        "success": False,
        "built": False,
        "asset_file_name": "arm64.deb", # Default, will change.
    }
    deb_results_amd64 = {
        "success": False,
        "built": False,
        "asset_file_name": "amd64.deb", # Default, will change.
    }
    deb_results_i386 = {
        "success": False,
        "built": False,
        "asset_file_name": "i386.deb", # Default, will change.
    }
    if is_debian_based():
        if is_arm64_toolchain_installed():
            results = package_deb(root_dir, version, sudo_pwd, "toolchain-linux-arm64.cmake", force_overwrite)
            deb_results_arm64.update(results)
            deb_results_arm64["built"] = True
            if not deb_results_arm64["success"]:
                print(f'Error: Packaging dist/{deb_results_arm64["asset_file_name"]} failed.')
                sys.exit(1)
        if is_x86_64_toolchain_installed():
            results = package_deb(root_dir, version, sudo_pwd, "toolchain-linux-x86_64.cmake", force_overwrite)
            deb_results_amd64.update(results)
            deb_results_amd64["built"] = True
            if not deb_results_amd64["success"]:
                print(f'Error: Packaging dist/{deb_results_amd64["asset_file_name"]} failed.')
                sys.exit(1)
        if is_i386_toolchain_installed():
            results = package_deb(root_dir, version, sudo_pwd, "toolchain-linux-i386.cmake", force_overwrite)
            deb_results_i386.update(results)
            deb_results_i386["built"] = True
            if not deb_results_i386["success"]:
                print(f'Error: Packaging dist/{deb_results_i386["asset_file_name"]} failed.')
                sys.exit(1)

    # A summary of everything that was done
    print(f"\n***********")
    print(f"* Summary *")
    print(f"***********")
    display_package_results(src_tar_gz_results)
    display_package_results(rpm_results)
    display_package_results(deb_results_arm64)
    display_package_results(deb_results_amd64)
    display_package_results(deb_results_i386)

    print(f"\nPackaging completed successfully.")

def package_windows_platform(root_dir: str, version: str, platform: str) -> dict:

    vcvarsall_args = []
    if platform == "x86_64":
        vcvarsall_args = ["amd64"]
    elif platform == "x86_32":
        vcvarsall_args = ["amd64_x86"]
    elif platform == "arm_64":
        vcvarsall_args = ["amd64_arm64"]
    elif platform == "arm_32":
        vcvarsall_args = ["amd64_arm"]

    call_vcvarsall(root_dir, vcvarsall_args)
    results = {
        "zip_results": {
            "success": False,
            "built": False,
            "asset_file_name": f"{platform}.zip", # Default, will change.
        },
        "msi_results": {
            "success": False,
            "built": False,
            "asset_file_name": f"{platform}.msi", # Default, will change.
        }
    }    
    zip_results = package_windows_zip(root_dir, version, platform)
    results["zip_results"].update(zip_results)
    results["zip_results"]["built"] = True    
    if not results["zip_results"]["success"]:
        print(f'Error: Packaging dist/{results["zip_results"]["asset_file_name"]} failed')
        sys.exit(1)

    # The zip file is better at detecting if the *content* is different.
    # If any changes are detected, it will force the creation and overwrite
    # of the .msi file in dist.
    force_msi_overwrite = False
    if results["zip_results"]["built"] and results["zip_results"]["copied"]:
        force_msi_overwrite = True

    # For now, skip the .msi file creation if wix is not installed.

    # Process the .msi file.
    if not is_wix_installed():
        print("Warning: WiX Toolset not found. MSI packaging skipped.")
    else:
        msi_results = package_windows_msi(root_dir, version, platform, force_msi_overwrite)
        results["msi_results"].update(msi_results)
        results["msi_results"]["built"] = True
        if not results["msi_results"]["success"]:
            print(f'Error: Packaging dist/{results["msi_results"]["asset_file_name"]} failed')
            sys.exit(1)

    return results

def package_all_windows(root_dir: str, version: str):
    results_x86_64 = package_windows_platform(root_dir, version, "x86_64")
    results_x86_32 = package_windows_platform(root_dir, version, "x86_32")

    # TODO: More testing needed for ARM platforms.
    #results_arm_64 = package_windows_platform(root_dir, version, "arm_64")
    #results_arm_32 = package_windows_platform(root_dir, version, "arm_32")

    print(f"\n***********")
    print(f"* Summary *")
    print(f"***********")
    display_package_results(results_x86_64["zip_results"])
    display_package_results(results_x86_64["msi_results"])
    display_package_results(results_x86_32["zip_results"])
    display_package_results(results_x86_32["msi_results"])
    #display_package_results(results_arm_64["zip_results"])
    #display_package_results(results_arm_64["msi_results"])
    #display_package_results(results_arm_32["zip_results"])
    #display_package_results(results_arm_32["msi_results"])

    print(f"Packaging completed successfully.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test release candidate assets in 'dist'")
    parser.add_argument('-p', '--pwd', type=str, default="", help="Optional password for sudo commands")
    args = parser.parse_args()

    sudo_pwd = args.pwd
    root_dir = verify_git_repo()
    is_updated, version = sync_versions(root_dir)
    is_updated, digest = sync_sources_digest(root_dir)
    host_platform = sys.platform
    if host_platform == "linux":
        package_all_linux(root_dir,version,sudo_pwd)
    elif host_platform == "win32":
        arch = platform.architecture()[0]
        if arch == '64bit':
            package_all_windows(root_dir, version)
        else:
            print( f"Unsupported [{arch}]. Only 64-bits windows supported for TA-Lib development.")
    else:
        print(f"Unsupported platform [{host_platform}]. Contact TA-Lib maintainers.")
        sys.exit(1)
