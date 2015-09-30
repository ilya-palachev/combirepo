#!/usr/bin/env python2.7
# -*- coding: utf-8 -*-
import os
import stat
import shutil
import sys
import logging
import re
from sets import Set
import subprocess
import urlparse
import hidden_subprocess
import multiprocessing
from rpmUtils.miscutils import splitFilename
import mic.kickstart
from mic.utils.misc import get_pkglist_in_comps
from dependency_graph_builder import DependencyGraphBuilder
import temporaries
import binfmt
import files
import check
import rpm_patcher
from repository import Repository, RepositoryData
from kickstart_parser import KickstartFile
from config_parser import ConfigParser
from repository_manager import RepositoryManager


repodata_regeneration_enabled = False
target_arhcitecture = None


def build_forward_dependencies(graph, package):
    """
    Builds the set of forward dependencies of the package.

    @param graph        The dependency graph of the repository.
    @param package      The name of package.

    @return             The set of forward dependencies + package itself
    """
    source = graph.get_name_id(package)
    logging.debug("Found id = {0} for package {1}".format(source, package))
    if source is None:
        logging.debug("Failed to find package {0} in dependency "
                      "tree.".format(package))
        return Set()
    dependencies = Set()
    for vertex in graph.bfsiter(source):
        dependency = graph.vs[vertex.index]["name"]
        logging.debug("Processing vertex {0}, its name is "
                      "{1}".format(vertex.index, dependency))
        dependencies.add(dependency)
    return dependencies


def build_package_set(graph, back_graph, package_names):
    """
    Builds the set of marked packages.

    @param graph            The dependency graph of the repository.
    @param back_graph       The backward dependency graph of the repository.
    @param package_names    The package names.

    @return                 The set of marked packages.
    """
    marked = Set()
    for package in package_names["forward"]:
        marked = marked | build_forward_dependencies(graph, package)
    for package in package_names["backward"]:
        marked = marked | build_forward_dependencies(back_graph, package)
    for package in package_names["single"]:
        if not graph.get_name_id(package) is None:
            marked = marked | Set([package])
    for package in package_names["excluded"]:
        if not graph.get_name_id(package) is None:
            marked = marked - Set([package])
    for package in package_names["service"]:
        if not graph.get_name_id(package) is None:
            marked = marked | Set([package])

    for package in marked:
        logging.info("Package {0} is marked".format(package))

    return marked


def construct_combined_repository(graph, marked_graph, marked_packages,
                                  if_mirror, rpm_patcher):
    """
    Constructs the temporary repository that consists of symbolic links to
    packages from non-marked and marked repositories.

    @param graph            Dependency graph of the non-marked repository
    @param marked_graph     Dependency graph of the marked repository
    @param marked_packages  Set of marked package names
    @param if_mirror        Whether to mirror not found marked packages from
                            non-marked repository
    @param rpm_patcher      The patcher of RPMs.

    @return             The path to the constructed combined repository.
    """
    repository_path = temporaries.create_temporary_directory("combirepo")
    packages_not_found = []

    for package in marked_packages:
        marked_package_id = marked_graph.get_name_id(package)
        if marked_package_id is None:
            packages_not_found.append(package)
            continue
        location_from = marked_graph.vs[marked_package_id]["location"]
        version_marked = marked_graph.vs[marked_package_id]["version"]
        release_marked = marked_graph.vs[marked_package_id]["release"]

        package_id = graph.get_name_id(package)
        if package_id is None:
            shutil.copy(location_from, repository_path)
        else:
            version = graph.vs[package_id]["version"]
            if version != version_marked:
                logging.error("Versions of package {0} differ: {1} and {2}. "
                              "Please go and rebuild the marked "
                              "package!".format(package, version,
                                                version_marked))
                sys.exit("Error.")
            release = graph.vs[package_id]["release"]
            if release != release_marked:
                logging.warning("Release numbers of package {0} differ: "
                                "{1} and {2}, so the marked package will be "
                                "patched so that to match to original release "
                                "number.".format(package, release,
                                                 release_marked))
                rpm_patcher.patch(location_from, repository_path, release)
            else:
                shutil.copy(location_from, repository_path)

    if len(packages_not_found) != 0:
        for package in packages_not_found:
            logging.error("Marked package {0} not found in marked "
                          "repository".format(package))
        if not if_mirror:
            logging.error("The above listed packages were not found in "
                          "marked repository.\n"
                          "HINT: use option -m to use non-marked packages "
                          "instead of them.")
            sys.exit("Error.")

    packages = Set(graph.vs["name"])
    for package in packages:
        if package in marked_packages:
            if package not in packages_not_found:
                continue
        if package in packages_not_found:
            logging.info("Package {0} from original repository will be "
                         "used.".format(package))
        package_id = graph.get_name_id(package)
        location_from = graph.vs[package_id]["location"]
        shutil.copy(location_from, repository_path)

    if logging.getLogger().getEffectiveLevel() == logging.DEBUG:
        hidden_subprocess.call(["ls", "-l", repository_path])

    return repository_path


def create_image(arch, repository_names, repository_paths, kickstart_file_path,
                 output_directory_path, mic_options, specific_packages):
    """
    Creates an image using MIC tool, from given repository and given kickstart
    file. It creates a copy of kickstart file and replaces "repo" to given
    repository path.

    @param arch                     The architecture of the image
    @param repository_names         The names of repositorues
    @param repository_paths         The repository paths
    @param kickstart_file           The kickstart file to be used
    @param output_directory_path    The path to the output directory
    @param mic_options              Additional options for MIC.
    @param specific_packages        Packages that must be additionally
                                    installed.
    """
    if repository_names is None or len(repository_names) == 0:
        raise Exception("Repository names are not given! "
                        "{0}".format(repository_names))
    modified_kickstart_file_path = temporaries.create_temporary_file("mod.ks")
    shutil.copy(kickstart_file_path, modified_kickstart_file_path)
    kickstart_file = KickstartFile(modified_kickstart_file_path)
    kickstart_file.replace_repository_paths(repository_names,
                                            repository_paths)
    kickstart_file.add_packages(specific_packages)

    # Now create the image using the "mic" tool:
    mic_command = ["sudo", "mic", "create", "loop",
                   modified_kickstart_file_path, "-A", arch, "-o",
                   output_directory_path, "--tmpfs", "--pkgmgr=zypp"]
    if logging.getLogger().getEffectiveLevel() <= logging.DEBUG:
        mic_options.extend(["--debug", "--verbose"])
    if mic_options is not None:
        mic_command.extend(mic_options)
    hidden_subprocess.call(mic_command)


def inform_about_unprovided(provided_symbols, unprovided_symbols,
                            marked_provided_symbols,
                            marked_unprovided_symbols):
    """
    Informs the user when any repository contains symbols that are not provided
    with any of them.

    @param provided_symbols             Symbols provided with non-marked
                                        repository
    @param unprovided_symbols           Symbols unprovided with non-marked
                                        repository
    @param marked_provided_symbols      Symbols provided with marked
                                        repository
    @param marked_unprovided_symbols    Symbols unprovided with marked
                                        repository
    """
    logging.debug("non-marked unprovided symbols: "
                  "{0}".format(unprovided_symbols))
    logging.debug("marked unprovided symbols: "
                  "{0}".format(marked_unprovided_symbols))
    lacking_symbols = unprovided_symbols - marked_provided_symbols
    marked_lacking_symbols = marked_unprovided_symbols - provided_symbols
    common_lacking_symbols = lacking_symbols & marked_lacking_symbols
    lacking_symbols = lacking_symbols - common_lacking_symbols
    marked_lacking_symbols = marked_lacking_symbols - common_lacking_symbols

    for symbol in common_lacking_symbols:
        logging.warning("Some packages in both repositories require symbol"
                        " {0}, but none of them provides it.".format(symbol))
    for symbol in lacking_symbols:
        logging.warning("Some packages in non-marked repository require symbol"
                        " {0}, but none of them provides it.".format(symbol))
    for symbol in marked_lacking_symbols:
        logging.warning("Some packages in marked repository require symbol"
                        " {0}, but none of them provides it.".format(symbol))


def process_repository_pair(repository_pair, builder, parameters,
                            rpm_patcher):
    """
    Processes one repository triplet and constructs combined repository for
    it.

    @param repository_pair      The repository pair.
    @param builder              The dependency graph builder.
    @param parameters           The parameters of the repository combiner.
    @param rpm_patcher          The patcher of RPMs.

    @return                     Path to combined repository.
    """
    repository_name = repository_pair.name
    logging.info("Processing repository \"{0}\"".format(repository_name))
    check.directory_exists(repository_pair.url)
    check.directory_exists(repository_pair.url_marked)

    strategy = parameters.prefer_strategy
    preferables = parameters.package_names["preferable"]
    graph, back_graph = builder.build_graph(repository_pair.url,
                                            parameters.architecture,
                                            preferables, strategy)
    # Generally speaking, sets of packages in non-marked and marked
    # repositories can differ. That's why we need to build graphs also for
    # marked repository.
    # Nevertheless we assume that graph of marked repository is isomorphic
    # to some subgraph of the non-marked repository graph.
    # FIXME: If it's not true in some pratical cases, then the special
    # treatment is needed.

    marked_graph, _ = builder.build_graph(repository_pair.url_marked,
                                          parameters.architecture,
                                          preferables,
                                          strategy)
    inform_about_unprovided(graph.provided_symbols, graph.unprovided_symbols,
                            marked_graph.provided_symbols,
                            marked_graph.unprovided_symbols)
    if parameters.greedy_mode:
        marked_packages = Set(marked_graph.vs["name"])
        for package in marked_packages:
            logging.debug("Package {0} is marked".format(package))
        for package in graph.vs["name"]:
            if package not in marked_packages:
                logging.debug("!!! Package {0} is NOT marked "
                              "!!!".format(package))
    else:
        marked_packages = build_package_set(graph, back_graph,
                                            parameters.package_names)
    repository = Repository(repository_pair.url)
    repository.prepare_data()
    repodata = repository.data
    mirror_mode = parameters.mirror_mode
    combined_repository_path = construct_combined_repository(graph,
                                                             marked_graph,
                                                             marked_packages,
                                                             mirror_mode,
                                                             rpm_patcher)
    combined_repository = Repository(combined_repository_path)
    combined_repository.set_data(repodata)
    combined_repository.generate_derived_data()
    return combined_repository_path, marked_packages


def regenerate_repodata(repository_path, marked_repository_path):
    """
    Re-generates the repodata for the given repository.

    @param repository_path  The path to the repository.

    Uses group.xml and patterns.xml from any path inside repository, if these
    files don't exist they're unpacked from package-groups.rpm
    """
    repository = Repository(repository_path)
    repodata = repository.get_data()
    if repodata.groups_data is None:
        logging.warning("There is no groups data in "
                        "{0}".format(repository_path))
    if repodata.patterns_data is None:
        logging.warning("There is no patterns data in "
                        "{0}".format(repository_path))
    repository.generate_derived_data()

    marked_repository = Repository(marked_repository_path)
    marked_repository.set_data(repodata)
    marked_repository.generate_derived_data()


def check_repository_names(names, kickstart_file_path):
    """
    Checks whether all names specified by user exist in the given kickstart
    file.

    @param names                The list of names specified by user.
    @param kickstart_file_path  The kickstart file.
    """
    kickstart_file = KickstartFile(kickstart_file_path)
    possible_names = kickstart_file.get_repository_names()
    if_error = False
    for name in names:
        if name not in possible_names:
            logging.error("Failed to find repository name "
                          "{0} in kickstart "
                          "file {1} specified "
                          "by user.".format(name, kickstart_file_path))
            logging.error("Possible names are: {0}".format(possible_names))
            if_error = True


def construct_combined_repositories(parameters, rpm_patcher, packages):
    """
    Constructs combined repositories based on arguments.

    @param parameters       The parameters of the repository combiner.
    @param rpm_patcher      The patcher of RPMs.
    @param packages         The list of package names that should be installed
                            to the image with all their dependencies.

    @return         The list of combined repositories' paths.
    """
    dependency_builder = DependencyGraphBuilder(check_rpm_name, packages)

    combined_repository_paths = []
    marked_packages_total = Set()
    for repository_pair in parameters.repository_pairs:
        logging.debug(parameters.package_names)
        path, marked_packages = process_repository_pair(repository_pair,
                                                        dependency_builder,
                                                        parameters,
                                                        rpm_patcher)
        marked_packages_total = marked_packages_total | marked_packages
        combined_repository_paths.append(path)

    specified_packages = []
    for key in ["forward", "backward", "single", "excluded"]:
        if parameters.package_names[key] is not None:
            specified_packages.extend(parameters.package_names[key])
    for package in specified_packages:
        if package not in marked_packages_total:
            raise Exception("Failed to find package with name \"{0}\" in any"
                            " of non-marked repositories".format(package))
    return combined_repository_paths


def initialize():
    """
    Initializes the repository combiner.
    """
    if os.geteuid() != 0:
        print("Changing user to SUDO user...")
        os.execvp("sudo", ["sudo"] + sys.argv)

    # These commands will be called in subprocesses, so we need to be sure
    # that they exist in the current environment:
    for command in ["mic", "createrepo", "modifyrepo", "sudo", "ls",
                    "rpm2cpio", "cpio"]:
        check.command_exists(command)


def check_rpm_name(rpm_name):
    """
    Checks whether the RPM with the given name has to be dowloaded and
    processed.

    @param rpm_name     The name of RPM package
    """
    file_name = None
    # If the given name is remote location, then we analyze whether we should
    # download it:
    url_parsed = urlparse.urlparse(rpm_name)
    if url_parsed.netloc is not None and len(url_parsed.netloc) > 0:
        logging.debug("Name {0} is detected as a URL "
                      "location {1}.".format(rpm_name, url_parsed))
        # All non-RPM files should be downloaded:
        if not rpm_name.endswith(".rpm"):
            return True
        # Not all RPMs should be downloaded:
        else:
            file_name = rpm_name.split('/')[-1].split('#')[0].split('?')[0]
    # The only other case when we process the file is the existing file in the
    # filesystem:
    elif os.path.isfile(rpm_name):
        file_name = os.path.basename(rpm_name)
    # In case if we are given RPM name from yum, we should make full name from
    # it before parsing:
    else:
        if not rpm_name.endswith(".rpm"):
            file_name = rpm_name + ".rpm"
        if os.path.basename(file_name) != file_name:
            file_name = os.path.basename(file_name)

    logging.debug("Processing argument {0} with RPM name "
                  "{1} ...".format(rpm_name, file_name))

    components = splitFilename(file_name)
    for component in components:
        if component is None:
            logging.error("Failed to parse argument {0} with RPM name "
                          "{1}, result is {2}!".format(rpm_name, file_name,
                                                       components))
            return False

    (name, version, release, epoch, architecture) = components
    if not architecture in [target_arhcitecture, "noarch"]:
        logging.debug("Target architecture is {0}".format(target_arhcitecture))
        logging.debug("It is indended for another architecture, skipping...")
        return False
    elif "debuginfo" in name or "debugsource" in name:
        logging.debug("It is debug package, skipping...")
        return False
    else:
        logging.debug("Passed...")
        return True


def get_kickstart_from_repos(repository_pairs, kickstart_substring):
    """
    Gets kickstart files from repositories that are used during the build.

    @param repository_pairs     The repository pairs used during the image
                                building.
    @param kickstart_substring  The substring that specifies the substring of
                                kickstart file name to be used.
    """
    if kickstart_substring is None:
        kickstart_substring = ""
    image_configurations_rpms = {}
    for repository_pair in repository_pairs:
        path = repository_pair.url
        rpms = files.find_fast(path, "image-configurations-.*\.rpm")
        image_configurations_rpms[repository_pair.name] = rpms
    logging.debug("Found following image-configurations RPMs: "
                  "{0}".format(image_configurations_rpms))

    kickstart_file_paths = {}
    for key in image_configurations_rpms.keys():
        for rpm in image_configurations_rpms[key]:
            directory_path = temporaries.create_temporary_directory("unpack")
            files.unrpm(rpm, directory_path)
            kickstart_file_paths[key] = files.find_fast(directory_path,
                                                        ".*.ks")
    logging.info("Found following kickstart files:")
    all_kickstart_file_paths = []
    for key in kickstart_file_paths.keys():
        logging.info(" * in repository {0}:".format(key))
        for kickstart_file_path in kickstart_file_paths[key]:
            basename = os.path.basename(kickstart_file_path)
            all_kickstart_file_paths.append(kickstart_file_path)
            logging.info("    * {0}".format(basename))
        if len(kickstart_file_paths[key]) == 0:
            logging.info("    <no kickstart files in this repository>")
    logging.debug("Found files: {0}".format(all_kickstart_file_paths))
    helper_string = "use option -k for that or \"kickstart = ...\" in config"
    kickstart_file_path_resulting = None
    if len(all_kickstart_file_paths) > 1:
        matching_kickstart_file_paths = []
        for kickstart_file_path in all_kickstart_file_paths:
            basename = os.path.basename(kickstart_file_path)
            if kickstart_substring in basename:
                matching_kickstart_file_paths.append(kickstart_file_path)
        if len(matching_kickstart_file_paths) > 1:
            logging.error("More than one kickstart files satisfy the "
                          "substring, or no substring was specified!")
            for kickstart_file_path in matching_kickstart_file_paths:
                basename = os.path.basename(kickstart_file_path)
                logging.error(" * {0}".format(basename))
            logging.error("Please, specified the unique name of kickstart "
                          "file or the unique substring! "
                          "({0}).".format(helper_string))
            sys.exit("Error.")
        else:
            kickstart_file_path_resulting = matching_kickstart_file_paths[0]
    elif len(all_kickstart_file_paths) == 1:
        kickstart_file_path_resulting = all_kickstart_file_paths[0]
    else:
        logging.error("No kickstart files found in repositories, please "
                      "specify the path to kickstart file manually! "
                      "({0}).".format(helper_string))
        sys.exit("Error.")
    return kickstart_file_path_resulting


def prepare_repositories(parameters):
    """
    Prepares repository pairs for use.

    @param parameters           The combirepo run-time parameters (for
                                explanation, see combirepo/parameters.py).
    @return                     The path to the used kickstart file.
    """
    repository_pairs = parameters.repository_pairs
    kickstart_file_path = parameters.kickstart_file_path
    cache_directory_path = parameters.temporary_directory_path
    if len(repository_pairs) == 0:
        raise Exception("No repository pairs given!")
    # Check that user has given correct arguments for repository names:
    names = [repository_pair.name for repository_pair in repository_pairs]
    logging.debug("Repository names: {0}".format(names))
    if kickstart_file_path is not None and os.path.isfile(kickstart_file_path):
        logging.info("Kickstart file {0} specified by user will be "
                     "used".format(kickstart_file_path))
        check_repository_names(names, kickstart_file_path)

    repository_cache_directory_path = os.path.join(cache_directory_path,
                                                   "repositories")
    if not os.path.isdir(repository_cache_directory_path):
        os.mkdir(repository_cache_directory_path)
    repository_manager = RepositoryManager(repository_cache_directory_path,
                                           check_rpm_name)
    path = repository_manager.prepare(parameters.sup_repo_url)
    parameters.sup_repo_url = path
    for repository_pair in repository_pairs:
        path = repository_manager.prepare(repository_pair.url)
        repository_pair.url = path
        path_marked = repository_manager.prepare(repository_pair.url_marked)
        repository_pair.url_marked = path_marked

    if repodata_regeneration_enabled:
        for repository_pair in parameters.repository_pairs:
            regenerate_repodata(repository_pair.url,
                                repository_pair.url_marked)
    if kickstart_file_path is None or not os.path.isfile(kickstart_file_path):
        kickstart_file_path = get_kickstart_from_repos(repository_pairs,
                                                       kickstart_file_path)
        check_repository_names(names, kickstart_file_path)
    logging.info("The following kickstart file will be used: "
                 "{0}".format(kickstart_file_path))
    return kickstart_file_path


def resolve_groups(repositories, kickstart_file_path):
    """
    Resolves packages groups from kickstart file.

    @param repositories         The list of original repository URLs.
    @param kickstart_file_path  The path to the kickstart file.
    @return                     The list of package names.
    """
    groups_paths = []
    for url in repositories:
        repository = Repository(url)
        repository.prepare_data()
        groups_data = repository.data.groups_data
        if groups_data is not None and len(groups_data) > 0:
            groups_path = temporaries.create_temporary_file("group.xml")
            with open(groups_path, "w") as groups_file:
                groups_file.writelines(groups_data)
            groups_paths.append(groups_path)
    logging.debug("Following groups files prepared:")
    for groups_path in groups_paths:
        logging.debug(" * {0}".format(groups_path))
    parser = mic.kickstart.read_kickstart(kickstart_file_path)
    groups = mic.kickstart.get_groups(parser)
    groups_resolved = {}
    for group in groups:
        for groups_path in groups_paths:
            packages = get_pkglist_in_comps(group.name, groups_path)
            if packages is not None and len(packages) > 0:
                groups_resolved[group.name] = packages
                break
        logging.debug("Group {0} contains {1} "
                      "packages.".format(group.name,
                                         len(groups_resolved[group.name])))
    packages_all = []
    for group_name in groups_resolved.keys():
        packages_all.extend(groups_resolved[group_name])
    if len(groups_resolved) != len(groups):
        logging.error("Not all groups were resolved.")
        sys.exit("Error.")
    return packages_all


def combine(parameters):
    """
    Combines the repostories based on parameters structure.

    @param parameters   The parameters of combirepo run.
    """
    global target_arhcitecture
    target_arhcitecture = parameters.architecture
    parameters.kickstart_file_path = prepare_repositories(parameters)

    original_repositories = [repository_pair.url for repository_pair
                             in parameters.repository_pairs]
    logging.debug("Original repository URLs: "
                  "{0}".format(original_repositories))
    packages = resolve_groups(original_repositories,
                              parameters.kickstart_file_path)
    logging.debug("Packages:")
    for package in packages:
        logging.debug(" * {0}".format(package))
    names = [repository_pair.name for repository_pair
             in parameters.repository_pairs]
    initialize()
    patcher = rpm_patcher.RpmPatcher(names,
                                     original_repositories,
                                     parameters.architecture,
                                     parameters.kickstart_file_path)
    patcher.prepare()
    combined_repositories = construct_combined_repositories(parameters,
                                                            patcher,
                                                            packages)
    mic_options = ["--shrink"]
    if parameters.mic_options is list:
        mic_options.extend(args.mic_options)
    hidden_subprocess.visible_mode = True

    ks_modified_path = temporaries.create_temporary_file("mod.ks")
    shutil.copy(parameters.kickstart_file_path, ks_modified_path)
    kickstart_file = KickstartFile(ks_modified_path)
    kickstart_file.add_repository_path("supplementary",
                                       parameters.sup_repo_url)
    parameters.kickstart_file_path = ks_modified_path
    create_image(parameters.architecture, names, combined_repositories,
                 parameters.kickstart_file_path,
                 parameters.output_directory_path,
                 mic_options,
                 parameters.package_names["service"])
    hidden_subprocess.visible_mode = False
