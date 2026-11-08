#!/bin/python

import os
import subprocess
import sys
import zipfile
from urllib.request import urlretrieve


# git diff should be empty, and there should be no errors
def check_git():
    result = subprocess.run(["git", "diff", "--name-only"], capture_output=True)
    if result.returncode != 0 or len(result.stdout) != 0:
        cont = input("uncommitted changes found, continue? [y/N] ")
        if cont != "y":
            sys.exit()


# download and update translations
def update_translations():
    import urllib.request
    url = "https://translate.codeberg.org/download/heliboard/?format=zip"
    zip_file_name = "translations.zip"
    req = urllib.request.Request(url, headers={'Cookie': 'x-robot-challenge-2=passed'})
    with urllib.request.urlopen(req) as response, open(zip_file_name, 'wb') as out_file:
        out_file.write(response.read())
    # extract all in heliboard/heliboard/app/src/main/res and heliboard/heliboard/fastlane/metadata
    with zipfile.ZipFile(zip_file_name, "r") as f:
        for file in f.filelist:
            # We ONLY want the app/src/main/res translations, NOT fastlane/metadata (which contains Heliboard's titles & descriptions)
            if not file.filename.startswith("heliboard/heliboard/app/src/main/res"):
                continue
            # Block extracting Heliboard's base string files, which would overwrite LeanType's English variants
            if file.filename.startswith("heliboard/heliboard/app/src/main/res/values/"):
                continue
            # Block extracting upstream release changelogs since LeanType tracks its own release cycle
            if "/changelogs/" in file.filename:
                continue
            file.filename = file.filename.replace("heliboard/heliboard/", "")
            f.extract(file)
    os.remove(zip_file_name)
    
    # Replace "HeliBoard" with "LeanType" in all extracted translation files
    # This ensures brand name consistency across all languages
    import re
    for root, dirs, files in os.walk("app/src/main/res"):
        for file in files:
            if file == "strings.xml":
                file_path = os.path.join(root, file)
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                # Replace HeliBoard with LeanType (preserving case variations)
                content = re.sub(r'HeliBoard', 'LeanType', content)
                content = re.sub(r'Heliboard', 'LeanType', content)
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(content)


# git diff to make sure default strings are the same
def check_default_values_diff():
    result = subprocess.run(["git", "diff", "--name-only", "app/src/main/res/values"], capture_output=True)
    if result.returncode != 0 or len(result.stdout) != 0:
        raise ValueError("default strings changed after translation import, something is wrong")


def read_dicts_readme() -> list[str]:
    dicts_readme_file = "../dictionaries/README.md"
    if os.path.isfile(dicts_readme_file):
        f = open(dicts_readme_file)
        lines = f.readlines()
        f.close()
        return lines
    readme_url = "https://codeberg.org/Helium314/aosp-dictionaries/raw/branch/main/README.md"
    tmp_readme = "dicts_readme_tmp.md"
    urlretrieve(readme_url, tmp_readme)
    f = open(tmp_readme)
    lines = f.readlines()
    f.close()
    os.remove(tmp_readme)
    return lines


# generate a list of dictionaries available in the dictionaries repository at (https://codeberg.org/Helium314/aosp-dictionaries
# for convenient linking when adding dictionaries in HeliBoard.
def update_dict_list():
    lines = read_dicts_readme()
    mode = 0
    dicts = []
    for line in lines:
        line = line.strip()
        if line.startswith("#"):
            mode = 0
        if line.startswith("# Dictionaries"):
            mode = 1
        if mode == 0 or not line.startswith("|") or line.startswith("| Language |") or line.startswith("| --- |"):
            continue
        split = [s.strip() for s in line.split("|")]
        if len(split) < 8:
            continue
        type_col = split[2]
        if "(" not in type_col or ")" not in type_col:
            continue
        url = type_col.split("(")[-1].split(")")[0]
        if "/raw/branch/main/" in url:
            rel_path = url.split("/raw/branch/main/")[-1]
        elif "/src/branch/main/" in url:
            rel_path = url.split("/src/branch/main/")[-1]
        else:
            rel_path = url.split("codeberg.org/Helium314/aosp-dictionaries/")[-1]

        filename = rel_path.split("/")[-1]
        dict_name = filename.split(".dict")[0]
        dict_type = dict_name.split("_")[0]
        rest = dict_name[len(dict_type) + 1:]

        kind_col = split[3].lower()
        source_col = split[7]

        if "__" in rest:
            loc_str, suffix = rest.split("__", 1)
            kind = "exp" if "experimental" in suffix else ("legacy" if "legacy" in suffix else suffix)
        else:
            loc_str = rest
            if "UNICODE LICENSE V3 (CLDR)" in source_col or "cldr" in rel_path:
                kind = "cldr"
            elif kind_col == "experimental" or kind_col == "yes":
                kind = "exp"
            elif kind_col == "legacy":
                kind = "legacy"
            else:
                kind = ""

        if "_" in loc_str:
            sp = loc_str.split("_")
            locale = sp[0] + "_" + "_".join(s.upper() for s in sp[1:])
        else:
            locale = loc_str

        dicts.append(f"{dict_type},{locale},{kind},{rel_path}\n")
    target_file = "app/src/main/assets/dictionaries_in_dict_repo.csv"
    # keep entries hosted outside the dictionaries repo (full https URLs, e.g. LeanType's own
    # release assets built by .github/workflows/build-dictionaries.yml), listed first for their locale
    own = []
    if os.path.exists(target_file):
        with open(target_file) as f:
            own = [l if l.endswith("\n") else l + "\n" for l in f if l.split(",", 3)[-1].startswith("https://")]
    merged = []
    for line in dicts:
        prefix = ",".join(line.split(",")[:2]) + ","
        merged += [o for o in own if o.startswith(prefix) and o not in merged]
        merged.append(line)
    merged += [o for o in own if o not in merged]
    with open(target_file, 'w') as f:
        f.writelines(merged)


# check whether there is a changelog file for current version and print result and version code
def check_changelog():
    changelog_dir = "fastlane/metadata/android/en-US/changelogs"
    assert os.path.isdir(changelog_dir)
    filenames = []
    for file in os.scandir(changelog_dir):
        filenames.append(file.name)
    filenames.sort()
    changelog_version = filenames[-1].replace(".txt", "")
    version = ""
    with open("app/build.gradle.kts") as f:
        for line in f:
            line = line.lstrip()
            if line.startswith("versionCode"):
                version = line.split(" ")[2].rstrip()
                break
    if changelog_version == version:
        print("changelog for", version, "exists")
    else:
        print("changelog for", version, "does not exist")


# update khipro mapping json, see discussion at the bottom of https://github.com/Helium314/HeliBoard/pull/2134
def update_khipro_mappings():
    source = "https://raw.githubusercontent.com/KhiproTeam/Khipro-Mappings/refs/heads/main/output/touchscreen.json"
    target = "app/src/main/assets/khipro-mappings.json"
    urlretrieve(source, target)


def main():
    if os.getcwd().endswith("tools"):
        os.chdir("../")
    check_git()
    update_translations()
    check_default_values_diff()
    update_dict_list()
    update_khipro_mappings()
    check_changelog()


if __name__ == "__main__":
    main()
