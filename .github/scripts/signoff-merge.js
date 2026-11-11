// SPDX-License-Identifier: GPL-3.0-or-later
//
// "/signoff" by comment: the repository owner certifies and squash-merges a pull
// request from anywhere, including a phone (see CONTRIBUTING.md and ADR 0012).
//
// Run by .github/workflows/signoff-merge.yml through actions/github-script. It
// never checks out or runs code from the pull request: it only reads the PR
// through the API and merges the exact head commit the owner was looking at.
//
// It merges through GitHub's asynchronous merge endpoint, which GitHub requires
// for pull requests in a stack (ADR 0024). For a stacked PR that endpoint also
// merges every open PR below it, with GitHub's own messages, so /signoff only
// ever merges a PR that targets the default branch: the bottom of a stack, which
// has nothing below it. Each PR in a stack is signed off on its own.

"use strict";

const COMMAND = "/signoff";

/** Decide whether a comment event may trigger a sign-off. Pure, for tests. */
function checkTrigger(payload, repositoryOwner) {
  const comment = payload.comment || {};
  if (!payload.issue || !payload.issue.pull_request) return "not a pull request";
  if ((comment.body || "").trim() !== COMMAND) return "not a /signoff command";
  if (!comment.user || comment.user.login !== repositoryOwner) return "only the repository owner can sign off";
  if (comment.author_association !== "OWNER") return "only the repository owner can sign off";
  return null;
}

/** Why a pull request can't be merged yet, or null if it can. Pure, for tests. */
function checkMergeable(pr, defaultBranch) {
  if (pr.state !== "open") return "the pull request isn't open";
  if (pr.draft) return "the pull request is a draft";
  if (pr.base.ref !== defaultBranch) {
    // In a stack, sign off the bottom PR first; the next one then targets the default branch.
    return `the pull request doesn't target ${defaultBranch}. In a stack, sign off the pull ` +
      `request below this one first (this one targets ${pr.base.ref}); once that has merged, ` +
      `change this one's base to ${defaultBranch} and comment /signoff again`;
  }
  if (pr.mergeable === false) return "the pull request has merge conflicts";
  if (pr.mergeable_state !== "clean") {
    return `the pull request isn't ready to merge (GitHub reports "${pr.mergeable_state}"; ` +
      "check that every required check has passed)";
  }
  return null;
}

/** The squash commit message: PR description, co-authors, then the owner's sign-off. */
function buildMessage(pr, commits, signer) {
  const signoff = `Signed-off-by: ${signer.name} <${signer.email}>`;
  // Drop any line in the description that could pass for a trailer we add.
  const body = (pr.body || "")
    .replace(/\r\n/g, "\n")
    .split("\n")
    .filter((line) => !/^\s*(signed-off-by|co-authored-by)\s*:/i.test(line))
    .join("\n")
    .trim();

  const coAuthors = [];
  const seen = new Set([signer.email.toLowerCase()]);
  for (const c of commits) {
    const author = c.commit && c.commit.author;
    if (!author || !author.email) continue;
    const key = author.email.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    coAuthors.push(`Co-authored-by: ${author.name} <${author.email}>`);
  }
  const trailers = [...coAuthors, signoff].join("\n");
  return {
    title: `${pr.title} (#${pr.number})`,
    message: body ? `${body}\n\n${trailers}` : trailers,
  };
}

/** The signer's identity, from their GitHub profile and no-reply address. */
function signerFor(user) {
  return {
    name: user.name || user.login,
    email: `${user.id}+${user.login}@users.noreply.github.com`,
  };
}

async function reply(github, context, number, text) {
  await github.rest.issues.createComment({
    ...context.repo,
    issue_number: number,
    body: `${text}\n\n---\n_Posted by the \`/signoff\` workflow._`,
  });
}

/** Wait for GitHub to finish computing mergeability (it can be null at first). */
async function getPullWhenSettled(github, context, number, attempts = 10, delayMs = 3000) {
  let pr;
  for (let i = 0; i < attempts; i++) {
    ({ data: pr } = await github.rest.pulls.get({ ...context.repo, pull_number: number }));
    if (pr.mergeable !== null && pr.mergeable_state !== "unknown") return pr;
    await new Promise((resolve) => setTimeout(resolve, delayMs));
  }
  return pr;
}

const MERGE_ASYNC = "PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge-async";
const MERGE_RESULT = "GET /repos/{owner}/{repo}/pulls/{pull_number}/merge-async/{uuid}";

/**
 * Squash-merge through the asynchronous endpoint and wait for the result.
 * Returns { status, sha?, message? }: "merged", "failed", "enqueued" or "pending"
 * (still running when we stopped waiting).
 */
async function mergeAsync(github, context, number, merge, options = {}) {
  const attempts = options.mergeAttempts ?? 40;
  const delayMs = options.mergeDelayMs ?? 3000;
  let result;
  try {
    ({ data: result } = await github.request(MERGE_ASYNC, {
      ...context.repo,
      pull_number: number,
      merge_action: "direct_merge", // never a merge queue: our message must be used
      merge_method: "squash",
      sha: merge.sha, // fails if the branch changed since we checked
      commit_title: merge.title,
      commit_message: merge.message,
      bypass_rules: false,
    }));
  } catch (error) {
    // 400 (not mergeable), 409 (a merge is already pending), 403, 404, 422.
    const data = error.response && error.response.data;
    const detail = (data && data.details && data.details.message) || (data && data.message) || error.message;
    if (error.status === 409) {
      return { status: "failed", message: `a merge is already in progress for this pull request (${detail})` };
    }
    return { status: "failed", message: detail };
  }
  for (let i = 0; result.status === "pending" && i < attempts; i++) {
    await new Promise((resolve) => setTimeout(resolve, delayMs));
    ({ data: result } = await github.request(MERGE_RESULT, {
      ...context.repo,
      pull_number: number,
      uuid: result.details.uuid,
    }));
  }
  const details = result.details || {};
  return { status: result.status, sha: details.sha, message: details.message };
}

/** Whether the commit GitHub made carries the sign-off line we asked for. */
async function hasSignoff(github, context, sha, signer) {
  const { data: commit } = await github.rest.repos.getCommit({ ...context.repo, ref: sha });
  const line = `Signed-off-by: ${signer.name} <${signer.email}>`;
  return (commit.commit.message || "").split("\n").some((l) => l.trim() === line);
}

async function run({ github, context, core }, options = {}) {
  const payload = context.payload;
  const refused = checkTrigger(payload, context.repo.owner);
  if (refused) {
    core.notice(`Ignored: ${refused}.`);
    return { merged: false, reason: refused };
  }
  const number = payload.issue.number;
  const commentedAt = payload.comment.created_at;

  const pr = await getPullWhenSettled(github, context, number, options.attempts, options.delayMs);
  // The owner signs off on what was there when they commented: refuse if the
  // branch moved afterwards.
  const { data: headCommit } = await github.rest.repos.getCommit({ ...context.repo, ref: pr.head.sha });
  const pushedAt = headCommit.commit.committer && headCommit.commit.committer.date;
  if (pushedAt && commentedAt && new Date(pushedAt) > new Date(commentedAt)) {
    const reason = "new commits arrived after your comment; review them and comment /signoff again";
    await reply(github, context, number, `Not merged: ${reason}.`);
    return { merged: false, reason };
  }

  const { data: repo } = await github.rest.repos.get(context.repo);
  const notReady = checkMergeable(pr, repo.default_branch);
  if (notReady) {
    await reply(github, context, number, `Not merged: ${notReady}.`);
    return { merged: false, reason: notReady };
  }

  const commits = await github.paginate(github.rest.pulls.listCommits, {
    ...context.repo,
    pull_number: number,
    per_page: 100,
  });
  const { data: user } = await github.rest.users.getByUsername({ username: payload.comment.user.login });
  const signer = signerFor(user);
  const { title, message } = buildMessage(pr, commits, signer);

  const outcome = await mergeAsync(github, context, number, { sha: pr.head.sha, title, message }, options);
  if (outcome.status === "failed") {
    await reply(github, context, number, `Not merged: GitHub refused the merge (${outcome.message}).`);
    return { merged: false, reason: outcome.message };
  }
  if (outcome.status === "pending") {
    const reason = "GitHub accepted the merge but hadn't finished it when the workflow stopped waiting";
    await reply(github, context, number, `Not merged yet: ${reason}. Check this pull request in a minute.`);
    return { merged: false, reason };
  }
  if (outcome.status !== "merged") {
    // "enqueued" can't happen with direct_merge; say so plainly if it ever does.
    const reason = `GitHub reported "${outcome.status}" instead of merging`;
    await reply(github, context, number, `Not merged: ${reason}.`);
    return { merged: false, reason };
  }
  // The sign-off must be in history: check the commit GitHub actually made.
  if (!outcome.sha || !(await hasSignoff(github, context, outcome.sha, signer))) {
    const reason = "merged, but the commit on the default branch doesn't carry the sign-off";
    await reply(github, context, number,
      `Merged, but **the commit on the default branch doesn't carry your Signed-off-by line** ` +
      `(${outcome.sha || "unknown commit"}). Record it as a DCO remediation (docs/dco-remediations.md).`);
    core.warning(reason);
    return { merged: true, signed: false, reason };
  }
  await github.rest.reactions.createForIssueComment({
    ...context.repo,
    comment_id: payload.comment.id,
    content: "+1",
  });
  core.notice(`Merged #${number} as ${outcome.sha} with ${signer.name}'s sign-off.`);
  return { merged: true, signed: true, sha: outcome.sha };
}

module.exports = { run, mergeAsync, checkTrigger, checkMergeable, buildMessage, signerFor, COMMAND };
