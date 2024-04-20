import json
import os
import re
import sys
import requests
import subprocess
import hashlib
import tarfile
from typing import Optional, Tuple


class Manifest:
    def __init__(self, path):
        self.name = ""
        self.repos = {}
        self.fetch = {}
        self.build = {}
        self.path = path

    def load(self):
        """
        A function to load data from a file specified by the path attribute.

        Loads the data from the file at the specified path and populates the instance attributes based on the loaded data.
        """
        if not os.path.exists(self.path):
            return

        with open(self.path, "r") as f:
            data = json.load(f)

            self.name = data["name"]

            if "repos" in data:
                self.repos = data["repos"]

            if "fetch" in data:
                self.fetch = data["fetch"]

            if "build" in data:
                self.build = data["build"]

    def get_repos(self) -> list:
        """
        Returns a list of keys from the repos dictionary.
        """
        return list(self.repos.keys())

    def get_packages(self, repo: str) -> Optional[list]:
        """
        Check if the repo is in the fetch data, return the list of packages if found.
        """
        if repo not in self.fetch:
            return None
        return list(self.fetch.get(repo))

    def get_all_packages(self) -> Optional[list]:
        """
        Returns a list of all packages defined in manifest
        """
        result = []
        
        for _, v in self.fetch.items():
            result = result + v

        for _, v in self.build.items():
            result = result + v

        return result if result.__len__() > 0 else None

    def get_build_list(self, src_source: str) -> Optional[list]:
        """
        A function that retrieves the build list based on the src_source parameter.

        Parameters:
            self: instance of the Manifest class
            src_source: a string representing the source to retrieve the build list from

        Returns:
            An optional list of build items associated with the src_source if found, otherwise None.
        """
        if src_source not in self.build:
            return None
        return list(self.build.get(src_source))


class Repository:
    def __init__(self, name: str, url: str):
        self.name = name
        self.url = url.replace('$repo', name)
        self.packages = {}
        self.refresh_database()

    def download_file(self, url, path):
        """
        A function to download a file from a given URL to a specified path.

        Parameters:
            self: instance of the class
            url: A string representing the URL of the file to be downloaded.
            path: A string representing the path to save the downloaded file.

        Returns:
            A string with the filename of the downloaded file.
        """
        if not os.path.exists(f"{path}"):
            os.mkdir(f"{path}")

        local_filename = url.split('/')[-1]
        # NOTE the stream=True parameter below
        if os.getenv("MOLYUU_REPO_FETCH_VIA_WGET") == "1":
            ret = os.system(f"wget {url} -O {path}/{local_filename}")
            if ret != 0:
                raise Exception(f"Failed to download {url}")
        else:
            with requests.get(url, stream=True) as r:
                r.raise_for_status()
                with open(f"{path}/{local_filename}", 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        # If you have chunk encoded response uncomment if
                        # and set chunk_size parameter to None.
                        # if chunk:
                        f.write(chunk)
        return local_filename

    def refresh_database(self):
        """
        Refreshes the database by fetching and parsing the database file. No parameters or return types specified.
        """
        database_folder = "workspace/repos/database"
        # Fetch Database
        self.download_file(
            f"{self.url}/{self.name}.db.tar.xz", database_folder)

        # Parse Database
        with tarfile.open(f"{database_folder}/{self.name}.db.tar.xz", 'r:xz') as tar:
            root_files = [member for member in tar.getmembers(
            ) if member.isdir() and member.name.count('/') == 0]
            folders = [member.name for member in root_files]
            for folder in folders:
                desc = tar.extractfile(f"{folder}/desc")
                desc_content = desc.read().decode("utf-8")

                package_name = re.search(
                    r'%NAME%\n(.+)\n', desc_content).group(1)
                package_file = re.search(
                    r'%FILENAME%\n(.+)\n', desc_content).group(1)
                package_sha256sum = re.search(
                    r'%SHA256SUM%\n(.+)\n', desc_content).group(1)
                self.packages[package_name] = {
                    "filename": package_file,
                    "sha256sum": package_sha256sum
                }

    def find_package(self, name) -> Optional[dict]:
        """
        Find a package based on the provided name.

        Parameters:
            name: A string representing the name of the package to find.

        Returns:
            An optional dictionary with the package information if found, otherwise None.
        """
        if name not in self.packages:
            return None

        return self.packages.get(name)

    def fetch_package(self, name, path) -> Optional[Tuple[str, str]]:
        """
        Fetches a package based on the provided name and path.

        Parameters:
            name: A string representing the name of the package to fetch.
            path: A string representing the path to save the fetched package.

        Returns:
            An optional tuple with filenames of fetched packages if successful, otherwise None.
        """
        package_info = self.find_package(name)
        package_debug_info = self.find_package(f"{name}-debug")

        if package_info is None:
            return None

        if not os.path.exists(f"{path}"):
            os.mkdir(f"{path}")

        # Download Package
        self.download_file(f"{self.url}/{package_info['filename']}", path)

        if package_debug_info is not None:
            self.download_file(
                f"{self.url}/{package_debug_info['filename']}", path)

        # Verify Package
        for pkg in [package_info, package_debug_info]:
            if pkg is None:
                continue

            with open(f"{path}/{pkg['filename']}", 'rb') as file:
                sha256 = hashlib.sha256()
                while chunk := file.read(1024):
                    sha256.update(chunk)

                if sha256.hexdigest() != pkg["sha256sum"]:
                    print(f"Package {name} failed verification.")
                    os.remove(f"{path}/{pkg['filename']}")
                    return None

        return (package_info["filename"], package_debug_info["filename"] if package_debug_info is not None else None)


class PackageGetter:
    def __init__(self, manifest: Manifest):
        self.repos = {}
        self.manifest = manifest
        self.pacman_known_packages = subprocess.check_output("pacman -Slq", shell=True).decode("utf-8").split("\n")[:-1]
        self.init_repos()

    def init_repos(self) -> bool:
        """
        Initialize repositories defined on the manifest.

        Returns:
            bool: True if all repositories were initialized successfully, False otherwise.
        """
        repo_list = self.manifest.get_repos()
        if repo_list is None:
            return False

        for repo_idx in range(0, repo_list.__len__()):
            repo = repo_list[repo_idx]
            print(
                f"Initializing repo {repo}...  [{repo_idx + 1}/{repo_list.__len__()}]")
            self.repos[repo] = Repository(repo, self.manifest.repos.get(repo))

        return True

    def fetch_packages_from_repos(self) -> bool:
        """
        Fetches packages from repositories based on the manifest.

        Returns:
            bool: True if all packages were fetched successfully, False otherwise.
        """
        for name, repo in self.repos.items():
            print("Fetching packages from repo " + name)
            package_list = self.manifest.get_packages(name)

            if package_list is None:
                continue

            for package_idx in range(0, package_list.__len__()):
                package = package_list[package_idx]
                print(
                    f"Fetching {package}...  [{package_idx + 1}/{package_list.__len__()}]")
                if repo.fetch_package(package, f"workspace/output") is None:
                    raise Exception(f"Package {package} not found!")

    def fetch_aur_packages_src(self) -> bool:
        """
        Fetches AUR packages source code based on the build list from the manifest.

        Returns:
            bool: True if all AUR packages were fetched successfully, False otherwise.
        """
        package_list = self.manifest.get_build_list("aur")
        if package_list is None:
            return False

        for package_idx in range(0, package_list.__len__()):
            package = package_list[package_idx]
            print(
                f"Fetching {package}...  [{package_idx + 1}/{package_list.__len__()}]")
            ret = os.system(
                f"git clone https://aur.archlinux.org/{package}.git workspace/build/{package}")
            if ret != 0:
                print(f"Package {package} failed to fetch.")
                raise Exception(f"Package {package} failed to fetch.")

        return True

    def fetch_remote_packages_src(self) -> bool:
        """
        Fetches remote packages source code based on the build list from the manifest.

        Returns:
            bool: True if all remote packages were fetched successfully, False otherwise.
        """
        package_list = self.manifest.get_build_list("remote")
        if package_list is None:
            return False

        for package_idx in range(0, package_list.__len__()):
            package_def = package_list[package_idx]
            remote_url = package_def["url"]

            package = os.path.basename(remote_url).replace(".git", "")
            if package == '':
                package = os.path.basename(remote_url[:-1]).replace(".git", "")
                if package == '':
                    raise Exception(f"Invalid package URL: {remote_url}")

            print(
                f"Fetching {package}...  [{package_idx + 1}/{package_list.__len__()}]")
            ret = os.system(
                f"git clone {remote_url} workspace/build/{package}")
            if ret != 0:
                print(f"Package {package} failed to fetch.")
                raise Exception(f"Package {package} failed to fetch.")

        return True

    def prepare_local_src(self) -> bool:
        """
        Prepares local source packages for building.
        Retrieves the list of local packages to build from the manifest, then copies each package to the build workspace directory.

        Returns:
            bool: True if all local packages were prepared successfully, False otherwise.
        """
        package_list = self.manifest.get_build_list("local")
        if package_list is None:
            return False

        for package_idx in range(0, package_list.__len__()):
            package = package_list[package_idx]
            print(
                f"Preparing {package}...  [{package_idx + 1}/{package_list.__len__()}]")
            ret = os.system(f"cp -r local/{package} workspace/build/{package}")
            if ret != 0:
                print(f"Package {package} failed to prepare.")
                raise Exception(f"Package {package} failed to prepare.")

        return True

    def install_build_deps(self, pkgbuild_dir: str, top: bool = True):
        """
        Install the dependencies for a package located in the given `pkgbuild_dir`.

        Parameters:
            pkgbuild_dir (str): The path to the directory containing the package's PKGBUILD file.
            top: Is this the top-level package? If so, we will copy related aur dependencies to ouput folder.

        Raises:
            Exception: If the dependencies for the package could not be installed.

        Returns:
            None
        """
        srcinfo = subprocess.check_output(f"cd {pkgbuild_dir} && makepkg --printsrcinfo", shell=True, text=True)
        pkgbase = srcinfo.replace("\t", "").split("\n\n")[0]
        depends = re.findall(r"^\s*depends = (.+)\n", pkgbase, re.MULTILINE)
        makedepends = re.findall(r"^\s*makedepends = (.+)\n", pkgbase, re.MULTILINE)
        checkdepends = re.findall(r"^\s*checkdepends = (.+)\n", pkgbase, re.MULTILINE)

        unresolved_deps = None        
        
        try:
            subprocess.check_output(f"pacman --color=always --deptest " + " ".join(depends + makedepends + checkdepends), shell=True, text=True)
        except subprocess.CalledProcessError as e:
            if e.returncode == 127:
                unresolved_deps = e.output.split("\n")
            else:
                raise Exception(f"Failed to install dependencies for {pkgbuild_dir}.")

        pacman_deps = []
        aur_deps = []

        if unresolved_deps != None:
            for dep in unresolved_deps:
                if dep == '':
                    continue
                if dep in self.pacman_known_packages:
                    pacman_deps.append(dep)
                else:
                    aur_deps.append(dep)

        # Install pacman dependencies
        if len(pacman_deps) > 0:
            print("Installing pacman build deps: " + " ".join(pacman_deps))
            ret = os.system(f"sudo pacman -S --noconfirm " + " ".join(pacman_deps))
            if ret != 0:
                raise Exception(f"Failed to install dependencies for {pkgbuild_dir}.")
            
        # Install AUR dependencies
        if len(aur_deps) > 0:
            for dep in aur_deps:
                result = json.loads(requests.get(f"https://aur.archlinux.org/rpc/?v=5&type=info&arg[]={dep}").content)
                if result["resultcount"] == 0:
                    raise Exception(f"Failed to install dependency: {dep} for {pkgbuild_dir}.")
                else:
                    matched = False
                    for possible_match in result["results"]:
                        if possible_match["Name"] == dep:
                            matched = True
                            ret = os.system(f"git clone https://aur.archlinux.org/{dep}.git workspace/build/{dep}")
                            if ret != 0:
                                raise Exception(f"Failed to install dependency: {dep} for {pkgbuild_dir}.")
                            self.install_build_deps(f"workspace/build/{dep}", False)
                            ret = os.system(f"cd workspace/build/{dep} && MAKEOPTS=\"-j$(nproc --all)\" makepkg -i --noconfirm")
                            if ret != 0:
                                raise Exception(f"Failed to install dependency: {dep} for {pkgbuild_dir}.")
                            if (dep in depends) and (dep not in self.manifest.get_all_packages() and top):
                                os.system(f"mv workspace/build/{dep}/*.pkg.tar.zst workspace/output/")
                            break

                    if not matched:
                        raise Exception(f"Failed to install dependency: {dep} for {pkgbuild_dir}.")
        
    def build_packages(self) -> bool:
        """
        Builds and fetches packages from the local, remote, and AUR repositories based on the manifest.
        """
        current_working_directory = os.getenv("PWD")
        self.fetch_aur_packages_src()
        self.fetch_remote_packages_src()
        self.prepare_local_src()

        if self.manifest.get_build_list("remote") is not None:
            package_defs = self.manifest.get_build_list("remote")
            for package_def_idx in range(0, package_defs.__len__()):
                package_def = package_defs[package_def_idx]
                remote_url = package_def["url"]
                package = os.path.basename(remote_url).replace(".git", "")
                if package == '':
                    package = os.path.basename(remote_url[:-1]).replace(".git", "")
                    if package == '':
                        raise Exception(f"Invalid package URL: {remote_url}")
                
                pkgbuilds = package_def["PKGBUILDs"]
                for pkgbuild in pkgbuilds:
                    subpkg = os.path.dirname(pkgbuild)
                    print(f"Building {package}::{subpkg}...  [{package_def_idx + 1}/{package_defs.__len__()}]")
                    self.install_build_deps(f"workspace/build/{package}/{subpkg}")
                    ret = os.system(f"cd workspace/build/{package}/{subpkg} && MAKEOPTS=\"-j$(nproc --all)\" makepkg --noconfirm")
                    if ret != 0:
                        print(f"Package {package}::{subpkg} failed to build.")
                        raise Exception(f"Package {package}::{subpkg} failed to build.")

                    os.system(f"mv workspace/build/{package}/{subpkg}/*.pkg.tar.zst workspace/output")

        for src in ["local", "aur"]:
            package_list = self.manifest.get_build_list(src)
            if package_list is None:
                continue

            for package_idx in range(0, package_list.__len__()):
                package = package_list[package_idx]
                print(
                    f"Building {package}...  [{package_idx + 1}/{package_list.__len__()}]")

                self.install_build_deps(f"workspace/build/{package}")
                ret = os.system(
                    f"cd workspace/build/{package} && MAKEOPTS=\"-j$(nproc --all)\" makepkg --noconfirm")

                if ret != 0:
                    print(f"Package {package} failed to build.")
                    raise Exception(f"Package {package} failed to build.")

                os.system(
                    f"mv workspace/build/{package}/*.pkg.tar.zst workspace/output")

        return True


def build_repository(name: str, sign: bool = False, password: str = "") -> bool:
    """
    A function to build a repository with the given name.

    Parameters:
        name: A string representing the name of the repository to build.

    Returns:
        bool: True if the repository is built successfully, False otherwise.
    """
    if sign and password != "":
        # Sign the repository
        ret = os.system(f"""
                        cd workspace/output
                        for i in *.pkg.tar.zst; do
                            echo {password} | gpg --detach-sign --pinentry-mode loopback --passphrase --passphrase-fd 0 --output $i.sig --sign $i
                        done
                        repo-add --sign -n -R {name}.db.tar.xz *.pkg.tar.zst
                        """)
        if ret != 0:
            print(f"Repository {name} failed to build.")
            raise Exception(f"Repository {name} failed to build.")
    else:
        ret = os.system(
            f"cd workspace/output && repo-add -n -R {name}.db.tar.xz *.pkg.tar.zst")
        if ret != 0:
            print(f"Repository {name} failed to build.")
            raise Exception(f"Repository {name} failed to build.")


def prepare_workspace():
    if os.path.exists("workspace"):
        print("Cleaning up workspace...")
        os.system(f"rm -rf workspace")

    os.mkdir("workspace")
    os.mkdir("workspace/repos")
    os.mkdir("workspace/build")
    os.mkdir("workspace/output")


def main(sign: bool = False, password: str = ""):
    prepare_workspace()
    manifest = Manifest("manifest.json")
    manifest.load()

    package_getter = PackageGetter(manifest)
    package_getter.fetch_packages_from_repos()
    package_getter.build_packages()

    build_repository(manifest.name, sign, password)


if __name__ == "__main__":
    argv = sys.argv
    if len(argv) == 3 and argv[1] == "--sign":
        main(True, argv[2])
    else:
        main()
