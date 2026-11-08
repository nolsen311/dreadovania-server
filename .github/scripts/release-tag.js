// SPDX-License-Identifier: GPL-3.0-or-later
//
// "/release" by comment: the repository owner releases a merged pull request
// from anywhere, including a phone (see CONTRIBUTING.md and the ADR on releasing
// by comment). The workflow tags the pull request's merge commit with the version
// in that commit's version file, then starts the release workflow on the tag.
//
// Run by .github/workflows/release-tag.yml through actions/github-script. It
// never checks out or runs code from the pull request: it reads everything
// through the API.

"use strict";

const COMMAND = "/release";
const VERSION = /^\d+\.\d+\.\d+$/;
const OK_CONCLUSIONS = new Set(["success", "skipped", "neutral"]);

/** Decide whether a comment event may trigger a release. Pure, for tests. */
function checkTrigger(payload, repositoryOwner) {
  const comment = payload.comment || {};
  if (!payload.issue || !payload.issue.pull_request) return "not a pull request";
  if ((comment.body || "").trim() !== COMMAND) return "not a /release command";
  if (!comment.user || comment.user.login !== repositoryOwner) return "only the repository owner can release";
  if (comment.author_association !== "OWNER") return "only the repository owner can release";
  return null;
}

/** Why a pull request can't be released, or null. Pure, for tests. */
function checkReleasable(pr, defaultBranch) {
  if (!pr.merged) return "the pull request isn't merged yet: comment /signoff first, then /release";
  if (pr.base.ref !== defaultBranch) return `the pull request wasn't merged into ${defaultBranch}`;
  if (!pr.merge_commit_sha) return "GitHub doesn't report a merge commit for this pull request";
  return null;
}

/** Read the project version from a version file's text. Pure, for tests. */
function readVersion(text, file) {
  const name = file.split("/").pop();
  let version;
  if (name === "package.json") {
    version = JSON.parse(text).version;
  } else if (name === "pyproject.toml" || name === "Cargo.toml") {
    const section = name === "pyproject.toml" ? "project" : "package";
    version = tomlSectionValue(text, section, "version");
  } else {
    version = text.trim(); // a plain VERSION file
  }
  if (typeof version !== "string" || !VERSION.test(version)) {
    throw new Error(`${file} doesn't hold a version like 1.2.3 (found ${JSON.stringify(version ?? null)})`);
  }
  return version;
}

function tomlSectionValue(text, section, key) {
  let inSection = false;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    const header = line.match(/^\[([^\]]+)\]$/);
    if (header) {
      inSection = header[1].trim() === section;
      continue;
    }
    const match = inSection && line.match(new RegExp(`^${key}\\s*=\\s*"([^"]*)"`));
    if (match) return match[1];
  }
  return undefined;
}

function parseVersion(v) {
  return v.split(".").map(Number);
}

function compareVersions(a, b) {
  const [x, y] = [parseVersion(a), parseVersion(b)];
  for (let i = 0; i < 3; i++) if (x[i] !== y[i]) return x[i] - y[i];
  return 0;
}

/** Why this version can't be tagged, given existing tag names, or null. Pure, for tests. */
function checkVersion(version, tagNames) {
  const tag = `v${version}`;
  if (tagNames.includes(tag)) return `${tag} already exists: bump the version in a new pull request first`;
  const released = tagNames.filter((t) => /^v\d+\.\d+\.\d+$/.test(t)).map((t) => t.slice(1));
  const newest = released.sort(compareVersions).at(-1);
  if (newest && compareVersions(version, newest) <= 0) {
    return `${tag} isn't newer than the latest release, v${newest}`;
  }
  return null;
}

/** Why the tested commit's checks don't allow a release, or null. Pure, for tests. */
function checkRuns(runs) {
  if (runs.length === 0) return "no checks ran on the pull request's last commit";
  const pending = runs.filter((r) => r.status !== "completed");
  if (pending.length) return `checks are still running (${pending.map((r) => r.name).join(", ")}); try again when they finish`;
  const failed = runs.filter((r) => !OK_CONCLUSIONS.has(r.conclusion));
  if (failed.length) return `checks didn't pass (${failed.map((r) => r.name).join(", ")})`;
  return null;
}

async function reply(github, context, number, text) {
  await github.rest.issues.createComment({
    ...context.repo,
    issue_number: number,
    body: `${text}\n\n---\n_Posted by the \`/release\` workflow._`,
  });
}

async function run({ github, context, core }, options = {}) {
  const versionFile = options.versionFile || "pyproject.toml";
  const releaseWorkflow = options.releaseWorkflow || "";
  const payload = context.payload;
  const refused = checkTrigger(payload, context.repo.owner);
  if (refused) {
    core.notice(`Ignored: ${refused}.`);
    return { released: false, reason: refused };
  }
  const number = payload.issue.number;
  const refuse = async (reason) => {
    await reply(github, context, number, `Not released: ${reason}.`);
    return { released: false, reason };
  };

  const { data: pr } = await github.rest.pulls.get({ ...context.repo, pull_number: number });
  const { data: repo } = await github.rest.repos.get(context.repo);
  const notReleasable = checkReleasable(pr, repo.default_branch);
  if (notReleasable) return refuse(notReleasable);
  const sha = pr.merge_commit_sha;

  // The merge commit must still be in the default branch's history.
  const { data: comparison } = await github.rest.repos.compareCommitsWithBasehead({
    ...context.repo,
    basehead: `${sha}...${repo.default_branch}`,
  });
  if (!["ahead", "identical"].includes(comparison.status)) {
    return refuse(`the merge commit isn't on ${repo.default_branch}`);
  }

  // Merges by the /signoff workflow's token don't run CI on the default branch,
  // so the evidence is the pull request's last commit, which was tested.
  const runs = await github.paginate(github.rest.checks.listForRef, {
    ...context.repo,
    ref: pr.head.sha,
    per_page: 100,
  });
  const failing = checkRuns(runs);
  if (failing) return refuse(failing);

  let version;
  try {
    const { data: file } = await github.rest.repos.getContent({ ...context.repo, path: versionFile, ref: sha });
    version = readVersion(Buffer.from(file.content, "base64").toString("utf8"), versionFile);
  } catch (error) {
    return refuse(`couldn't read the version from ${versionFile}: ${error.message}`);
  }
  const tags = await github.paginate(github.rest.repos.listTags, { ...context.repo, per_page: 100 });
  const badVersion = checkVersion(version, tags.map((t) => t.name));
  if (badVersion) return refuse(badVersion);

  const tag = `v${version}`;
  const actor = payload.comment.user.login;
  try {
    // An annotated tag, so the tag itself records who released it and from where.
    const { data: tagObject } = await github.rest.git.createTag({
      ...context.repo,
      tag,
      object: sha,
      type: "commit",
      message: `${context.repo.repo} ${version}\n\nReleased by @${actor} with /release on #${number}.`,
    });
    await github.rest.git.createRef({ ...context.repo, ref: `refs/tags/${tag}`, sha: tagObject.sha });
  } catch (error) {
    return refuse(`GitHub refused to create ${tag} (${error.message}). Check that the tag ruleset lets GitHub Actions create version tags`);
  }

  const runsUrl = `${context.serverUrl || "https://github.com"}/${context.repo.owner}/${context.repo.repo}/actions`;
  let started = "";
  if (releaseWorkflow) {
    // A tag created with the workflow token doesn't trigger workflows by itself;
    // a workflow_dispatch does, and runs on the tag (GITHUB_REF=refs/tags/vX.Y.Z).
    try {
      await github.rest.actions.createWorkflowDispatch({ ...context.repo, workflow_id: releaseWorkflow, ref: tag });
      started = ` The release workflow is starting: ${runsUrl}/workflows/${releaseWorkflow}`;
    } catch (error) {
      await reply(github, context, number,
        `Tagged ${tag} at ${sha}, but the release workflow couldn't be started (${error.message}). ` +
        `Start it from the Actions tab on the ${tag} tag.`);
      return { released: false, tagged: tag, reason: error.message };
    }
  }
  await github.rest.reactions.createForIssueComment({ ...context.repo, comment_id: payload.comment.id, content: "+1" });
  await reply(github, context, number, `Tagged ${tag} at ${sha}.${started}`);
  core.notice(`Tagged ${tag} at ${sha} for @${actor}.`);
  return { released: true, tag, sha };
}

module.exports = {
  run,
  checkTrigger,
  checkReleasable,
  readVersion,
  checkVersion,
  checkRuns,
  compareVersions,
  COMMAND,
};
