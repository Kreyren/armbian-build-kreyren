#! /bin/env python3
import email.utils
import logging
import mailbox
import os
import re
import subprocess
import tempfile

import git  # GitPython
from unidecode import unidecode
from unidiff import PatchSet

REGEX_PATCH_FILENAMES = r"^patching file \"(.+)\""
log: logging.Logger = logging.getLogger("patching_utils")


class PatchRootDir:
	def __init__(self, abs_dir, root_type, patch_type, root_dir):
		self.abs_dir = abs_dir
		self.root_type = root_type
		self.patch_type = patch_type
		self.root_dir = root_dir


class PatchSubDir:
	def __init__(self, rel_dir, sub_type):
		self.rel_dir = rel_dir
		self.sub_type = sub_type


class PatchDir:
	def __init__(self, patch_root_dir: PatchRootDir, patch_sub_dir: PatchSubDir, abs_root_dir: str):
		self.patch_root_dir: PatchRootDir = patch_root_dir
		self.patch_sub_dir: PatchSubDir = patch_sub_dir
		self.full_dir = os.path.realpath(os.path.join(self.patch_root_dir.abs_dir, self.patch_sub_dir.rel_dir))
		self.rel_dir = os.path.relpath(self.full_dir, abs_root_dir)
		self.root_type = self.patch_root_dir.root_type
		self.sub_type = self.patch_sub_dir.sub_type
		self.patch_files: list[PatchFileInDir] = []
		self.is_autogen_dir: bool = False

	def __str__(self) -> str:
		return "<PatchDir: full_dir:'" + str(self.full_dir) + "'>"

	def find_series_patch_files(self) -> list["PatchFileInDir"]:
		# do nothing if the self.full_path is not a real, existing, directory
		if not os.path.isdir(self.full_dir):
			return []

		# If the directory contains a series.conf file.
		series_patches: list[PatchFileInDir] = []
		series_conf_path = os.path.join(self.full_dir, "series.conf")
		if os.path.isfile(series_conf_path):
			counter = 0
			patches_in_series = self.parse_series_conf(series_conf_path)
			for patch_file_name in patches_in_series:
				patch_file_path = os.path.join(self.full_dir, patch_file_name)
				if os.path.isfile(patch_file_path):
					counter += 1
					patch_file = PatchFileInDir(patch_file_path, self)
					patch_file.from_series = True
					patch_file.series_counter = counter
					# Fix basename for patches in series.conf
					relative_path = os.path.relpath(patch_file_path, self.full_dir)
					patch_file.relative_dirs_and_base_file_name = os.path.splitext(relative_path)[0]
					series_patches.append(patch_file)
				else:
					raise Exception(
						f"series.conf file {series_conf_path} contains a patch file {patch_file_name} that does not exist")
		return series_patches

	def find_files_patch_files(self) -> list["PatchFileInDir"]:
		# do nothing if the self.full_path is not a real, existing, directory
		if not os.path.isdir(self.full_dir):
			return []

		# Find the files in self.full_dir that end in .patch; do not consider subdirectories.
		# Add them to self.patch_files.
		for file in os.listdir(self.full_dir):
			# noinspection PyTypeChecker
			if file.endswith(".patch"):
				self.patch_files.append(PatchFileInDir(file, self))
		return self.patch_files

	@staticmethod
	def parse_series_conf(series_conf_path):
		patches_in_series = []
		with open(series_conf_path, "r") as series_conf_file:
			for line in series_conf_file:
				line = line.strip()
				if line.startswith("#"):
					continue
				# if line begins with "-", skip it
				if line.startswith("-"):
					continue
				if line == "":
					continue
				patches_in_series.append(line)
		return patches_in_series


class PatchFileInDir:
	def __init__(self, file_name, patch_dir: PatchDir):
		self.file_name = file_name
		self.patch_dir: PatchDir = patch_dir
		self.relative_dirs_and_base_file_name = os.path.splitext(self.file_name)[0]
		self.file_name_no_ext_no_dirs = os.path.basename(self.relative_dirs_and_base_file_name)
		self.from_series = False
		self.series_counter = None

	def __str__(self) -> str:
		desc: str = f"<PatchFileInDir: file_name:'{self.file_name}', dir:{self.patch_dir.__str__()} >"
		return desc

	def full_file_path(self):
		return os.path.join(self.patch_dir.full_dir, self.file_name)

	def relative_to_src_filepath(self):
		return os.path.join(self.patch_dir.rel_dir, self.file_name)

	def split_patches_from_file(self) -> list["PatchInPatchFile"]:
		# Hack: for autogen dirs, we just need to be as fast as possible, don't parse anything.
		if self.patch_dir.is_autogen_dir:
			contents_bytes = read_file_as_bytes(self.full_file_path())
			# @TODO: date?
			bare_patch = PatchInPatchFile(
				self, 1, "", f"Autogenerated patch",
				f"Armbian Autopatcher <auto.patch@armbian.com>",
				f"[AUTOGEN] {self.relative_dirs_and_base_file_name}", None)
			bare_patch.diff_bytes = contents_bytes
			log.warning(f"Patch file {self.full_file_path()} is autogenerated.")
			return [bare_patch]

		counter: int = 1
		mbox: mailbox.mbox = mailbox.mbox(self.full_file_path())
		is_invalid_mbox: bool = False

		# Sanity check: if the file is understood as mailbox, make sure the first line is a valid "From " line,
		# and has the magic marker 'Mon Sep 17 00:00:00 2001' in it; otherwise, it could be a combined
		# bare patch + mbox-formatted patch in a single file, and we'd lose the bare patch.
		if len(mbox) > 0:
			contents, contents_read_problems = read_file_as_utf8(self.full_file_path())
			first_line = contents.splitlines()[0].strip()
			if not first_line.startswith("From ") or "Mon Sep 17 00:00:00 2001" not in first_line:
				# is_invalid_mbox = True # we might try to recover from this is there's too many
				# log.error(
				raise Exception(
					f"File {self.full_file_path()} seems to be a valid mbox file, but it begins with"
					f" '{first_line}', but in mbox the 1st line should be a valid From: header"
					f" with the magic date.")
			# Obtain how many times the magic marker date string is present in the contents
			magic_marker_count = contents.count("Mon Sep 17 00:00:00 2001")
			if magic_marker_count != len(mbox):
				# is_invalid_mbox = True # we might try to recover from this is there's too many
				# log.error(
				raise Exception(
					f"File {self.full_file_path()} seems to be a valid mbox file, but it contains"
					f" {magic_marker_count} magic marker dates, while the mbox file has been parsed as"
					f" {len(mbox)} patches. Check the file for mbox formatting errors.")

		# if there is no emails, it's a diff-only patch file.
		if is_invalid_mbox or len(mbox) == 0:
			# read the file into a string; explicitly use utf-8 to not depend on the system locale
			diff, read_problems = read_file_as_utf8(self.full_file_path())
			bare_patch = PatchInPatchFile(self, counter, diff, None, None, None, None)
			bare_patch.problems.append("not_mbox")
			bare_patch.problems.extend(read_problems)
			log.warning(f"Patch file {self.full_file_path()} is not properly mbox-formatted.")
			return [bare_patch]

		# loop over the emails in the mbox
		patches: list[PatchInPatchFile] = []
		msg: mailbox.mboxMessage
		for msg in mbox:
			patch: str = msg.get_payload()
			# split the patch itself and the description from the payload
			desc, patch_contents = self.split_description_and_patch(patch)
			if len(patch_contents) == 0:
				log.warning(
					f"WARNING: patch file {self.full_file_path()} fragment {counter} contains an empty patch")
				continue

			# Sanity check: if the patch_contents contains the magic marker, something is _very_ wrong, and we're gonna eat a patch.
			if "Mon Sep 17 00:00:00 2001" in patch_contents:
				raise Exception(
					f"File {self.full_file_path()} fragment {counter} seems to be a valid mbox file, but it contains"
					f" the magic date in the patch contents, shouldn't happen. Check the mbox formatting.")

			patches.append(PatchInPatchFile(
				self, counter, patch_contents, desc, msg['From'], msg['Subject'], msg['Date']))

			counter += 1

		# sanity check, throw exception if there are no patches
		if len(patches) == 0:
			raise Exception("No valid patches found in file " + self.full_file_path())
		return patches

	@staticmethod
	def split_description_and_patch(full_message_text: str) -> tuple["str | None", str]:
		separator = "\n---\n"
		# check if the separator is in the patch, if so, split
		if separator in full_message_text:
			# find the _last_ occurrence of the separator, and split two chunks from that position
			separator_pos = full_message_text.rfind(separator)
			desc = full_message_text[:separator_pos]
			patch = full_message_text[separator_pos + len(separator):]
			return desc, patch
		else:  # no separator, so no description, patch is the full message
			desc = None
			patch = full_message_text
		return desc, patch

	def rewrite_patch_file(self, patches: list["PatchInPatchFile"]):
		# Produce a mailbox file from the patches.
		# The patches are assumed to be in the same order as they were in the original file.
		# The original file is overwritten.
		output_file = self.full_file_path()
		log.info(f"Rewriting {output_file} with new patches...")
		with open(output_file, "w") as f:
			for patch in patches:
				log.info(f"Writing patch {patch.counter} to {output_file}...")
				f.write(patch.rewritten_patch)


# Placeholder for future manual work
def shorten_patched_file_name_for_stats(path):
	return os.path.basename(path)


def parse_patch_stdout_for_files(stdout_output: str):
	# run the REGEX_PATCH_FILENAMES on the output; get the group 1 (the filename) for each match
	# log.debug(f"Running regex on {stdout_output}")
	ret: list[str] = re.findall(REGEX_PATCH_FILENAMES, stdout_output, re.MULTILINE)
	# log.debug(f"Found {len(ret)} patched files in patch output: {','.join(ret)}.")
	return ret  # warning: this includes files the patch *deleted* too


class PatchInPatchFile:

	def __init__(self, parent: PatchFileInDir, counter: int, diff: str, desc, from_hdr, sbj_hdr, date_hdr):
		self.problems: list[str] = []
		self.applied_ok: bool = False
		self.rewritten_patch: str | None = None
		self.git_commit_hash: str | None = None
		self.actually_patched_files: list[str] = []

		self.parent: PatchFileInDir = parent
		self.counter: int = counter
		self.diff: str | None = diff
		self.diff_bytes: bytes | None = None

		self.failed_to_parse: bool = False

		# Basic parsing of properly mbox-formatted patches
		self.desc: str = downgrade_to_ascii(desc) if desc is not None else None
		self.from_name, self.from_email = self.parse_from_name_email(from_hdr) if from_hdr is not None else (
			None, None)
		self.subject: str = downgrade_to_ascii(fix_patch_subject(sbj_hdr)) if sbj_hdr is not None else None
		self.date = email.utils.parsedate_to_datetime(date_hdr) if date_hdr is not None else None

		self.patched_file_stats_dict: dict = {}
		self.total_additions: int = 0
		self.total_deletions: int = 0
		self.files_modified: int = 0
		self.files_added: int = 0
		self.files_renamed: int = 0
		self.files_removed: int = 0
		self.created_file_names = []
		self.deleted_file_names = []
		self.all_file_names_touched = []

	def parse_from_name_email(self, from_str: str) -> tuple["str | None", "str | None"]:
		m = re.match(r'(?P<name>.*)\s*<\s*(?P<email>.*)\s*>', from_str)
		if m is None:
			self.problems.append("invalid_author")
			log.warning(
				f"Failed to parse name and email from: '{from_str}' while parsing patch {self.counter} in file {self.parent.full_file_path()}")
			return downgrade_to_ascii(remove_quotes(from_str)), "unknown-email@domain.tld"
		else:
			# Return the name and email
			return downgrade_to_ascii(remove_quotes(m.group("name"))), remove_quotes(m.group("email"))

	def one_line_patch_stats(self) -> str:
		files_desc = ", ".join(self.patched_file_stats_dict)
		return f"{self.text_diffstats()} {{{files_desc}}}"

	def text_diffstats(self) -> str:
		operations: list[str] = []
		operations.append(f"{self.files_modified}M") if self.files_modified > 0 else None
		operations.append(f"{self.files_added}A") if self.files_added > 0 else None
		operations.append(f"{self.files_removed}D") if self.files_removed > 0 else None
		operations.append(f"{self.files_renamed}R") if self.files_renamed > 0 else None
		return f"(+{self.total_additions}/-{self.total_deletions})[{', '.join(operations)}]"

	def parse_patch(self):
		# Hack: don't parse if autogenned; this could also be "don't parse if larger than X megabytes" since
		# large patches cause trouble
		if self.parent.patch_dir.is_autogen_dir:
			log.warning(
				f"Skipping parsing of auto-generated patch {self.counter} in file {self.parent.full_file_path()}")
			return
		else:
			# parse the patch, using the unidiff package
			try:
				patch = PatchSet(self.diff, encoding=None)
			except Exception as e:
				self.problems.append("invalid_diff")
				self.failed_to_parse = True
				log.error(
					f"Failed to parse unidiff for file {self.parent.full_file_path()}(:{self.counter}): {str(e).strip()}")
				return  # no point in continuing; the patch is invalid; might be recovered during apply

		self.total_additions = 0
		self.total_deletions = 0
		self.files_renamed = 0
		self.files_modified = len(patch.modified_files)
		self.files_added = len(patch.added_files)
		self.files_removed = len(patch.removed_files)
		self.created_file_names = [f.path for f in patch.added_files]
		self.deleted_file_names = [f.path for f in patch.removed_files]
		self.all_file_names_touched = \
			[f.path for f in patch.added_files] + \
			[f.path for f in patch.modified_files] + \
			[f.path for f in patch.removed_files]
		self.patched_file_stats_dict = {}
		for f in patch:
			if not f.is_binary_file:
				self.total_additions += f.added
				self.total_deletions += f.removed
				self.patched_file_stats_dict[shorten_patched_file_name_for_stats(f.path)] = {
					"abs_changed_lines": f.added + f.removed}
			self.files_renamed = self.files_renamed + 1 if f.is_rename else self.files_renamed
		# sort the self.patched_file_stats_dict by the abs_changed_lines, descending
		self.patched_file_stats_dict = dict(sorted(
			self.patched_file_stats_dict.items(),
			key=lambda item: item[1]["abs_changed_lines"],
			reverse=True))
		# sanity check; if all the values are zeroes, throw an exception
		if self.total_additions == 0 and self.total_deletions == 0 and \
			self.files_modified == 0 and self.files_added == 0 and self.files_removed == 0:
			self.problems.append("diff_has_no_changes")
			raise Exception(
				f"Patch file {self.parent.full_file_path()} has no changes. diff is {len(self.diff)} bytes: '{self.diff}'")

	def __str__(self) -> str:
		desc: str = \
			f"<{self.parent.relative_dirs_and_base_file_name}(:{self.counter}):" + \
			f"{self.one_line_patch_stats()}: {self.from_email}: '{self.subject}' >"
		return desc

	def apply_patch(self, working_dir: str, options: dict[str, bool]):
		# Sanity check: if patch would create files, make sure they don't exist to begin with.
		# This avoids patches being able to overwrite the mainline.
		for would_be_created_file in self.created_file_names:
			full_path = os.path.join(working_dir, would_be_created_file)
			if os.path.exists(full_path):
				self.problems.append("overwrites")
				log.warning(
					f"File {would_be_created_file} already exists, but patch {self} would re-create it.")
				if options["allow_recreate_existing_files"]:
					log.warning(f"Tolerating recreation in {self} as instructed.")
					os.remove(full_path)

		# Use the 'patch' utility to apply the patch.
		if self.diff_bytes is None:
			real_input = self.diff.encode("utf-8")
		else:
			real_input = self.diff_bytes

		# create a temporary filename (don't create the file yet: patch will maybe create it)
		rejects_file = tempfile.mktemp()
		# log.debug(f"Rejects file is going to be '{rejects_file}'...")

		proc = subprocess.run(
			["patch", "--batch", "-p1", "-N", f"--reject-file={rejects_file}", "--quoting-style=c"],
			cwd=working_dir,
			input=real_input,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			check=False)
		# read the output of the patch command
		stdout_output = proc.stdout.decode("utf-8").strip()
		stderr_output = proc.stderr.decode("utf-8").strip()
		# if stdout_output != "":
		# log.debug(f"patch stdout: {stdout_output}")
		# if stderr_output != "":
		# log.warning(f"patch stderr: {stderr_output}")

		# Check if the rejects exists:
		if os.path.exists(rejects_file):
			log.warning(f"Rejects file {rejects_file} exists.")
			# Show its contents
			with open(rejects_file, "r") as f:
				log.warning(f"Rejects file contents: {f.read()}")
			# delete it
			os.remove(rejects_file)

		# Look at stdout. If it contains:
		if " (offset" in stdout_output or " with fuzz " in stdout_output:
			log.warning(f"Patch {self} needs rebase: offset/fuzz used during apply.")
			self.problems.append("needs_rebase")

		if "can't find file to patch at input line" in stdout_output:
			log.warning(f"Patch {self} needs review: can't find file to patch.")
			self.problems.append("missing_file")

		# parse the stdout output for the files actually patched.
		if options["set_patch_date"]:
			self.actually_patched_files = parse_patch_stdout_for_files(stdout_output)
			self.apply_patch_date_to_files(working_dir, options)

		# Check if the exit code is not zero and bomb
		if proc.returncode != 0:
			# prefix each line of the stderr_output with "STDERR: ", then join again
			stderr_output = "\n".join([f"STDERR: {line}" for line in stderr_output.splitlines()])
			stderr_output = "\n" + stderr_output if stderr_output != "" else stderr_output
			stdout_output = "\n".join([f"STDOUT: {line}" for line in stdout_output.splitlines()])
			stdout_output = "\n" + stdout_output if stdout_output != "" else stdout_output
			self.problems.append("failed_apply")
			raise Exception(
				f"Failed to apply patch {self.parent.full_file_path()}:{stderr_output}{stdout_output}")

	def commit_changes_to_git(self, repo: git.Repo, add_rebase_tags: bool, split_patches: bool):
		log.info(f"Committing changes to git: {self.parent.relative_dirs_and_base_file_name}")
		# add all the files that were touched by the patch
		# if the patch failed to parse, this will be an empty list, so we'll just add all changes.
		add_all_changes_in_git = False
		if (not self.failed_to_parse) and (not self.parent.patch_dir.is_autogen_dir):
			# sanity check.
			if len(self.all_file_names_touched) == 0:
				raise Exception(
					f"Patch {self} has no files touched, but is not marked as failed to parse.")
			# add all files to git staging area
			all_files_to_add: list[str] = []
			for file_name in self.all_file_names_touched:
				is_delete = False
				# Check if deleted, don't complain if so.
				if file_name in self.deleted_file_names:
					is_delete = True

				log.info(f"Adding file {file_name} to git")
				full_path = os.path.join(repo.working_tree_dir, file_name)
				if (not os.path.exists(full_path)) and (not is_delete):
					self.problems.append("wrong_strip_level")
					log.error(f"File '{full_path}' does not exist, but is touched by {self}")
					add_all_changes_in_git = True
				else:
					all_files_to_add.append(file_name)

			if split_patches:
				return self.commit_changes_to_git_grouped(all_files_to_add, repo)

			if not add_all_changes_in_git:
				repo.git.add("-f", all_files_to_add)

		if self.failed_to_parse or self.parent.patch_dir.is_autogen_dir or add_all_changes_in_git:
			log.warning(f"Rescue: adding all changed files to git for {self}")
			repo.git.add(repo.working_tree_dir)

		desc_no_none = self.desc if self.desc is not None else ""
		commit_message = f"{self.subject}\n\n{desc_no_none}"

		if add_rebase_tags:
			commit_message = f"{self.parent.relative_dirs_and_base_file_name}(:{self.counter})\n\nOriginal-Subject: {self.subject}\n{self.desc}"
			commit_message = f"{commit_message}\n{self.patch_rebase_tags_desc()}"

		author: git.Actor = git.Actor(self.from_name, self.from_email)
		committer: git.Actor = git.Actor("Armbian AutoPatcher", "patching@armbian.com")
		commit = repo.index.commit(
			message=commit_message,
			author=author,
			committer=committer,
			author_date=self.date,
			commit_date=self.date,
			skip_hooks=True
		)
		log.info(f"Committed changes to git: {commit.hexsha}")
		# Make sure the commit is not empty
		if commit.stats.total["files"] == 0:
			self.problems.append("empty_commit")
			raise Exception(
				f"Commit {commit.hexsha} ended up empty; source patch is {self} at {self.parent.full_file_path()}(:{self.counter})")
		return {"commit_hash": commit.hexsha, "patch": self}

	def commit_changes_to_git_grouped(self, all_files_to_add: list[str], repo: git.Repo):
		all_commits = []
		prefix = "Feiteng "
		grouped_files = {}
		# group files by directory
		for file_name in all_files_to_add:
			dir_name = os.path.dirname(file_name)
			if dir_name not in grouped_files:
				grouped_files[dir_name] = []
			grouped_files[dir_name].append(file_name)

		for group_name, files in grouped_files.items():
			for one_file in files:
				repo.git.add(one_file)

			commit_message = f"{prefix}{group_name}\n\n{prefix}{group_name}"
			author: git.Actor = git.Actor("Ricardo Pardini", "ricardo@pardini.net")
			commit = repo.index.commit(
				message=commit_message,
				author=author,
				committer=author,
				author_date=self.date,
				commit_date=self.date,
				skip_hooks=True
			)
			log.info(f"Committed changes to git: {commit.hexsha}")
			# Make sure the commit is not empty
			if commit.stats.total["files"] == 0:
				self.problems.append("empty_commit")
				raise Exception(
					f"Commit {commit.hexsha} ended up empty; source patch is {self} at {self.parent.full_file_path()}(:{self.counter})")
			all_commits.append({"commit_hash": commit.hexsha, "patch": self})
		return all_commits

	def patch_rebase_tags_desc(self):
		tags = {}
		tags["Patch-File"] = self.parent.relative_dirs_and_base_file_name
		tags["Patch-File-Counter"] = self.counter
		tags["Patch-Rel-Directory"] = self.parent.patch_dir.rel_dir
		tags["Patch-Type"] = self.parent.patch_dir.patch_root_dir.patch_type
		tags["Patch-Root-Type"] = self.parent.patch_dir.root_type
		tags["Patch-Sub-Type"] = self.parent.patch_dir.sub_type
		if self.subject is not None:
			tags["Original-Subject"] = self.subject
		ret = ""
		for k, v in tags.items():
			ret += f"X-Armbian: {k}: {v}\n"
		return ret

	def markdown_applied(self):
		if self.applied_ok:
			return "✅"
		return "❌"

	def markdown_problems(self):
		ret = []
		# if it's a patch in a series, add emoji
		if self.parent.from_series:
			ret.append(f" 📜 ")

		if self.parent.patch_dir.is_autogen_dir:
			ret.append(f" 🤖 ")

		if len(self.problems) == 0:
			ret.append("✅ ")

		for problem in self.problems:
			if problem in ["not_mbox", "needs_rebase"]:
				# warning emoji
				ret.append(f"⚠️`[{problem}]` ")
			elif problem in ["autogen"]:
				ret.append(f"ℹ️`[{problem}]` ")
			else:
				ret.append(f"❌`[{problem}]` ")

		# if it's a user patch, add smiley
		if self.parent.patch_dir.patch_root_dir.root_type == "user":
			ret.append(" 🫠`[user]` ")

		return " ".join(ret)

	def markdown_diffstat(self):
		return f"`{self.text_diffstats()}`"

	def markdown_files(self):
		ret = []
		max_files_shown = 15
		# Use the keys of the patch_file_stats_dict which is already sorted by the larger files
		file_names = list(self.patched_file_stats_dict.keys())
		# if no files were touched, just return an ?
		if len(file_names) == 0:
			return "`?`"
		for file_name in file_names[:max_files_shown]:
			ret.append(f"`{file_name}`")
		if len(file_names) > max_files_shown:
			ret.append(f"_and {len(file_names) - max_files_shown} more_")
		return ", ".join(ret)

	def markdown_author(self):
		if self.from_name:
			return f"`{self.from_name.strip()}`"
		return "`[no Author]`"

	def markdown_subject(self):
		if self.subject:
			return f"_{self.subject}_"
		return "`[no Subject]`"

	def markdown_link_to_patch(self):
		if self.git_commit_hash is None:
			return ""
		return f"{self.git_commit_hash} "

	def markdown_name(self):
		ret = []
		patch_name = self.parent.relative_dirs_and_base_file_name
		# if the basename includes slashes, split after the last slash, the first part is the directory, second the file
		if "/" in self.parent.relative_dirs_and_base_file_name:
			dir_name, patch_name = self.parent.relative_dirs_and_base_file_name.rsplit("/", 1)
			if dir_name is not None:
				ret.append(f"`[{dir_name}/]`")
		ret.append(f"`{patch_name}`")
		return " ".join(ret)

	def apply_patch_date_to_files(self, working_dir, options):
		# The date applied to the patched files is:
		# 1) The date of the root Makefile
		# 2) The date of the patch file
		# And date is 2, unless 1 is higher.
		patch_mtime = os.path.getmtime(self.parent.full_file_path())
		makefile_mtime = options["root_makefile_date"]
		final_mtime = makefile_mtime
		if patch_mtime > makefile_mtime:
			log.debug(f"Patch {self.parent.full_file_path()} is newer than root Makefile, using patch date")
			final_mtime = patch_mtime
		else:
			log.warn(
				f"Root Makefile is newer than patch '{self.parent.full_file_path()}', using Makefile date")
		# Apply the date to all files that were touched by the patch
		# If the patch parsed OK, avoid trying to touch files the patch deleted.
		files_to_touch = self.actually_patched_files
		if not self.failed_to_parse:
			# remove self.deleted_file_names files_to_touch
			files_to_touch = [f for f in files_to_touch if f not in self.deleted_file_names]

		for file_name in files_to_touch:
			# log.debug(f"Setting mtime of '{file_name}' to '{final_mtime}'.")
			file_path = os.path.join(working_dir, file_name)
			try:
				os.utime(file_path, (final_mtime, final_mtime))
			except FileNotFoundError:
				log.error(f"File '{file_path}' not found in patch {self}, can't set mtime.")


def fix_patch_subject(subject):
	# replace newlines with one space
	subject = re.sub(r"\s+", " ", subject.strip())
	# replace every non-printable character with a space
	subject = re.sub(r"[^\x20-\x7e]", " ", subject)
	# replace two consecutive spaces with one
	subject = re.sub(r" {2}", " ", subject).strip()
	# remove tags from the beginning of the subject
	tags = ['PATCH']
	for tag in tags:
		# subject might begin with "[tag xxxxx]"; remove it
		if subject.startswith(f"[{tag}"):
			subject = subject[subject.find("]") + 1:].strip()
	prefixes = ['FROMLIST(v1): ']
	for prefix in prefixes:
		if subject.startswith(prefix):
			subject = subject[len(prefix):].strip()
	return subject


# This is definitely not the right way to do this, but it works for now.
def prepare_clean_git_tree_for_patching(repo: git.Repo, revision_sha: str, branch_name: str):
	# Let's find the Commit object for the revision_sha
	log.info("Resetting git tree to revision '%s'", revision_sha)
	commit = repo.commit(revision_sha)
	# Lets checkout, detached HEAD, to that Commit
	repo.head.reference = commit
	repo.head.reset(index=True, working_tree=True)
	# Let's create a new branch, and checkout to it, discarding any existing branch
	log.info("Creating branch '%s'", branch_name)
	repo.create_head(branch_name, revision_sha, force=True)
	repo.head.reference = repo.heads[branch_name]
	repo.head.reset(index=True, working_tree=True)
	# Let's remove all the untracked, but not ignored, files from the working copy
	for file in repo.untracked_files:
		full_name = os.path.join(repo.working_tree_dir, file)
		log.info(f"Removing untracked file '{file}'")
		os.remove(full_name)


def export_commit_as_patch(repo: git.Repo, commit: str):
	# Export the commit as a patch
	proc = subprocess.run([
		"git", "format-patch",
		"--unified=3",  # force 3 lines of diff context
		"--keep-subject",  # do not add a prefix to the subject "[PATCH] "
		# "--add-header=Organization: Armbian",  # add a header to the patch (ugly, changes the header)
		"--no-encode-email-headers",  # do not encode email headers
		# "--signature=66666" # add a signature; this does not work and causes patch to not be emitted.
		'--signature', "Armbian",
		'--zero-commit',  # do not use the git revision, instead 000000...0000
		'--stat=120',  # 'wider' stat output; default is 80
		'--stat-graph-width=10',  # shorten the diffgraph graph part, it's too long
		"-1", "--stdout", commit
	],
		cwd=repo.working_tree_dir,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		check=False)
	# read the output of the patch command
	stdout_output = proc.stdout.decode("utf-8")
	stderr_output = proc.stderr.decode("utf-8")
	# Check if the exit code is not zero and bomb
	if proc.returncode != 0:
		raise Exception(f"Failed to export commit {commit} to patch: {stderr_output}")
	if stdout_output == "":
		raise Exception(f"Failed to export commit {commit} to patch: no output")
	return stdout_output


# Hack
def downgrade_to_ascii(utf8: str) -> str:
	return unidecode(utf8)


def remove_quotes(utf8: str) -> str:
	return utf8.replace('"', '')


# Try hard to read a possibly invalid utf-8 file
def read_file_as_utf8(file_name: str) -> tuple[str, list[str]]:
	with open(file_name, "rb") as f:
		content = f.read()  # Read the file as bytes
		try:
			return content.decode("utf-8"), []  # no problems if this worked
		except UnicodeDecodeError as ude:
			log.warning(f"File '{file_name}' is not valid utf-8, trying to fix it...: '{ude}'")
			# If decoding failed, try to decode as iso-8859-1
			return content.decode("iso-8859-1"), ["invalid_utf8"]  # utf-8 problems


def read_file_as_bytes(file_name: str) -> bytes:
	with open(file_name, "rb") as f:
		return f.read()  # Read the file as bytes


# Extremely Armbian-specific.
def perform_git_archeology(
	base_armbian_src_dir: str, armbian_git_repo: git.Repo, patch: PatchInPatchFile,
	bad_archeology_hexshas: list[str], fast: bool) -> bool:
	log.info(f"Trying to recover description for {patch.parent.file_name}:{patch.counter}")
	file_name_for_search = f"{patch.parent.file_name_no_ext_no_dirs}.patch"

	patch_file_paths: list[str] = []
	if fast:
		patch_file_paths = [patch.parent.full_file_path()]
	else:
		# Find all the files in the repo with the same name as the patch file.
		# Use the UNIX find command to find all the files with the same name as the patch file.
		proc = subprocess.run(
			[
				"find", base_armbian_src_dir,
				"-name", file_name_for_search,
				"-type", "f"
			],
			cwd=base_armbian_src_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
		patch_file_paths = proc.stdout.decode("utf-8").splitlines()
	log.info(f"Found {len(patch_file_paths)} files with name {file_name_for_search}")
	all_commits: list = []
	for found_file in patch_file_paths:
		relative_file_path = os.path.relpath(found_file, base_armbian_src_dir)
		hexshas = armbian_git_repo.git.log('--pretty=%H', '--follow', '--', relative_file_path) \
			.split('\n')
		log.info(f"- Trying to recover description for {relative_file_path} from {len(hexshas)} commits")

		# filter out hexshas that are in the known-bad archeology list
		hexshas = [hexsha for hexsha in hexshas if hexsha.strip() != '' and hexsha not in bad_archeology_hexshas]

		log.debug(f"hexshas: {hexshas}")

		commits = [armbian_git_repo.rev_parse(c) for c in hexshas]
		all_commits.extend(commits)

	unique_commits: list[git.Commit] = []
	for commit in all_commits:
		if commit not in unique_commits:
			unique_commits.append(commit)

	unique_commits.sort(key=lambda c: c.committed_datetime)

	if len(unique_commits) == 0:
		log.warning(f"Could not find any commits for '{file_name_for_search}'.")
		return False

	main_suspect: git.Commit = unique_commits[0]
	log.info(f"- Main suspect: {main_suspect}: {main_suspect.message.rstrip()} Author: {main_suspect.author}")

	# From the main_suspect, set the subject and the author, and the dates.
	main_suspect_msg_lines = main_suspect.message.splitlines()
	# strip each line
	main_suspect_msg_lines = [line.strip() for line in main_suspect_msg_lines]
	# remove empty lines
	main_suspect_msg_lines = [line for line in main_suspect_msg_lines if line != ""]
	main_suspect_subject = main_suspect_msg_lines[0].strip()
	# remove the first line, which is the subject
	suspect_desc_lines = main_suspect_msg_lines[1:]

	# Now, create a list for all other non-main suspects.
	other_suspects_desc: list[str] = []
	other_suspects_desc.extend(
		[f"> recovered message: > {suspect_desc_line}" for suspect_desc_line in suspect_desc_lines])
	other_suspects_desc.extend("")
	for commit in unique_commits:
		subject = commit.message.splitlines()[0].strip()
		rfc822_date = commit.committed_datetime.strftime("%a, %d %b %Y %H:%M:%S %z")
		other_suspects_desc.extend([
			f"- Revision {commit.hexsha}: https://github.com/armbian/build/commit/{commit.hexsha}",
			f"  Date: {rfc822_date}",
			f"  From: {commit.author.name} <{commit.author.email}>",
			f"  Subject: {subject}",
			""
		])

	patch.desc = downgrade_to_ascii("\n".join([f"> X-Git-Archeology: {line}" for line in other_suspects_desc]))

	if patch.subject is None:
		patch.subject = downgrade_to_ascii("[ARCHEOLOGY] " + main_suspect_subject)
	if patch.date is None:
		patch.date = main_suspect.committed_datetime
	if patch.from_name is None or patch.from_email is None:
		patch.from_name, patch.from_email = downgrade_to_ascii(
			main_suspect.author.name), main_suspect.author.email
	return True
