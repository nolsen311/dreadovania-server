// SPDX-License-Identifier: GPL-3.0-or-later
// Tests for signoff-merge.js. Run: node --test .github/scripts/
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { run, checkTrigger, checkMergeable, buildMessage, signerFor } = require("./signoff-merge.js");

const OWNER = "nolsen311";

function payload(overrides = {}) {
  return {
    issue: { number: 7, pull_request: { url: "x" } },
    comment: {
      id: 99,
      body: "/signoff",
      user: { login: OWNER },
      author_association: "OWNER",
      created_at: "2026-11-08T10:00:00Z",
      ...overrides.comment,
    },
    ...overrides.top,
  };
}

const goodPr = {
  number: 7,
  title: "Add the thing",
  body: "What and why.\r\n\r\nSigned-off-by: Mallory <m@evil.example>\nCo-authored-by: Fake <f@x>\nMore text.",
  state: "open",
  draft: false,
  base: { ref: "main" },
  head: { sha: "abc123" },
  mergeable: true,
  mergeable_state: "clean",
};

// ---- trigger --------------------------------------------------------------------

test("only an exact /signoff comment by the owner on a PR triggers", () => {
  assert.equal(checkTrigger(payload(), OWNER), null);
  assert.equal(checkTrigger(payload({ comment: { body: "  /signoff \n" } }), OWNER), null);
});

test("other comments, other users and plain issues are ignored", () => {
  assert.match(checkTrigger(payload({ comment: { body: "/signoff please" } }), OWNER), /not a \/signoff/);
  assert.match(checkTrigger(payload({ comment: { body: "LGTM /signoff" } }), OWNER), /not a \/signoff/);
  assert.match(checkTrigger(payload({ comment: { user: { login: "mallory" } } }), OWNER), /owner/);
  assert.match(
    checkTrigger(payload({ comment: { author_association: "COLLABORATOR" } }), OWNER),
    /owner/,
  );
  assert.match(checkTrigger({ ...payload(), issue: { number: 7 } }, OWNER), /not a pull request/);
});

// ---- readiness -------------------------------------------------------------------

test("a clean, open, non-draft PR against the default branch is mergeable", () => {
  assert.equal(checkMergeable(goodPr, "main"), null);
});

test("anything else is refused with a reason", () => {
  assert.match(checkMergeable({ ...goodPr, state: "closed" }, "main"), /isn't open/);
  assert.match(checkMergeable({ ...goodPr, draft: true }, "main"), /draft/);
  assert.match(checkMergeable({ ...goodPr, base: { ref: "dev" } }, "main"), /target main/);
  assert.match(checkMergeable({ ...goodPr, base: { ref: "dev" } }, "main"), /below this one first/);
  assert.match(checkMergeable({ ...goodPr, mergeable: false }, "main"), /conflicts/);
  assert.match(checkMergeable({ ...goodPr, mergeable_state: "blocked" }, "main"), /"blocked"/);
  assert.match(checkMergeable({ ...goodPr, mergeable_state: "unstable" }, "main"), /required check/);
});

// ---- message ------------------------------------------------------------------------

const signer = signerFor({ id: 64344, login: OWNER, name: "Nate Olsen" });

test("the signer is the commenter's GitHub identity", () => {
  assert.deepEqual(signer, { name: "Nate Olsen", email: "64344+nolsen311@users.noreply.github.com" });
  assert.equal(signerFor({ id: 1, login: "anon", name: null }).name, "anon");
});

test("the message ends with co-authors and exactly one real sign-off", () => {
  const commits = [
    { commit: { author: { name: "Claude", email: "noreply@anthropic.com" } } },
    { commit: { author: { name: "Claude", email: "NOREPLY@anthropic.com" } } }, // duplicate
    { commit: { author: { name: "Nate Olsen", email: signer.email } } }, // the signer
  ];
  const { title, message } = buildMessage(goodPr, commits, signer);
  assert.equal(title, "Add the thing (#7)");
  const lines = message.split("\n");
  assert.equal(lines.at(-1), "Signed-off-by: Nate Olsen <64344+nolsen311@users.noreply.github.com>");
  assert.equal(lines.at(-2), "Co-authored-by: Claude <noreply@anthropic.com>");
  // Trailer-like lines copied from the PR description are removed: only ours remain.
  assert.equal(message.match(/^signed-off-by:/gim).length, 1);
  assert.ok(!message.includes("Mallory"));
  assert.ok(!message.includes("Fake <f@x>"));
  assert.ok(message.startsWith("What and why."));
  assert.ok(!message.includes("\r"));
});

test("an empty description still produces the trailers", () => {
  const { message } = buildMessage({ ...goodPr, body: null }, [], signer);
  assert.equal(message, `Signed-off-by: Nate Olsen <${signer.email}>`);
});

// ---- the whole flow, against a fake GitHub API -------------------------------------------

function fakeGitHub({
  pr = goodPr,
  headCommitDate = "2026-11-08T09:00:00Z",
  mergeError,
  results = [{ status: "pending", details: { uuid: "u-1" } }, { status: "merged", details: { message: "ok", sha: "m3rged" } }],
  mergedMessage,
} = {}) {
  const calls = { merge: [], polls: [], comments: [], reactions: [], warnings: [] };
  const queue = [...results];
  const github = {
    paginate: async (_fn, _args) => [{ commit: { author: { name: "Claude", email: "noreply@anthropic.com" } } }],
    request: async (route, args) => {
      if (route.startsWith("PUT ") && route.endsWith("/merge-async")) {
        if (mergeError) throw mergeError;
        calls.merge.push(args);
        return { data: queue.shift() };
      }
      if (route.startsWith("GET ") && route.includes("/merge-async/")) {
        calls.polls.push(args);
        return { data: queue.shift() };
      }
      throw new Error(`unexpected route ${route}`);
    },
    rest: {
      pulls: {
        get: async () => ({ data: pr }),
        listCommits: async () => ({ data: [] }),
        merge: async () => {
          throw new Error("the synchronous merge endpoint must not be used");
        },
      },
      repos: {
        get: async () => ({ data: { default_branch: "main" } }),
        getCommit: async ({ ref }) =>
          ref === pr.head.sha
            ? { data: { commit: { committer: { date: headCommitDate } } } }
            : { data: { commit: { message: mergedMessage ?? calls.merge[0].commit_message } } },
      },
      users: { getByUsername: async () => ({ data: { id: 64344, login: OWNER, name: "Nate Olsen" } }) },
      issues: { createComment: async (args) => calls.comments.push(args) },
      reactions: { createForIssueComment: async (args) => calls.reactions.push(args) },
    },
  };
  return { github, calls };
}

const core = { notice: () => {}, warning: () => {} };
const context = (p = payload()) => ({ payload: p, repo: { owner: OWNER, repo: "r" } });
const fast = { attempts: 1, delayMs: 0, mergeAttempts: 3, mergeDelayMs: 0 };

test("a valid /signoff squash-merges the exact head commit with the sign-off", async () => {
  const { github, calls } = fakeGitHub();
  const result = await run({ github, context: context(), core }, fast);
  assert.deepEqual(result, { merged: true, signed: true, sha: "m3rged" });
  assert.equal(calls.merge.length, 1);
  const merge = calls.merge[0];
  assert.equal(merge.merge_method, "squash");
  assert.equal(merge.merge_action, "direct_merge"); // never a merge queue's own message
  assert.equal(merge.bypass_rules, false);
  assert.equal(merge.sha, "abc123");
  assert.equal(merge.pull_number, 7);
  assert.match(merge.commit_message, /Signed-off-by: Nate Olsen <64344\+nolsen311@users\.noreply\.github\.com>$/);
  assert.deepEqual(calls.polls.map((p) => p.uuid), ["u-1"]); // waited for the result
  assert.equal(calls.reactions[0].content, "+1");
  assert.equal(calls.comments.length, 0);
});

test("a merge GitHub finishes at once needs no waiting", async () => {
  const { github, calls } = fakeGitHub({ results: [{ status: "merged", details: { message: "ok", sha: "s" } }] });
  assert.equal((await run({ github, context: context(), core }, fast)).merged, true);
  assert.equal(calls.polls.length, 0);
});

test("a merge that fails in the background is explained", async () => {
  const { github, calls } = fakeGitHub({
    results: [{ status: "pending", details: { uuid: "u" } }, { status: "failed", details: { message: "Required status check \"Test\" is expected" } }],
  });
  const result = await run({ github, context: context(), core }, fast);
  assert.equal(result.merged, false);
  assert.match(calls.comments[0].body, /Required status check/);
  assert.equal(calls.reactions.length, 0);
});

test("a merge still running when the workflow stops waiting says so", async () => {
  const pending = { status: "pending", details: { uuid: "u" } };
  const { github, calls } = fakeGitHub({ results: [pending, pending, pending, pending, pending] });
  const result = await run({ github, context: context(), core }, fast);
  assert.equal(result.merged, false);
  assert.match(calls.comments[0].body, /Not merged yet/);
  assert.equal(calls.polls.length, 3);
});

test("a merge commit without the sign-off is flagged, not celebrated", async () => {
  const { github, calls } = fakeGitHub({ mergedMessage: "Add the thing\n\nCo-authored-by: Claude <noreply@anthropic.com>" });
  const result = await run({ github, context: context(), core }, fast);
  assert.deepEqual([result.merged, result.signed], [true, false]);
  assert.match(calls.comments[0].body, /doesn't carry your Signed-off-by line/);
  assert.equal(calls.reactions.length, 0);
});

test("the bottom of a stack can be signed off; a PR above it can't", async () => {
  // GitHub's asynchronous merge of a stacked PR also merges the open PRs below it,
  // with its own messages, so only a PR that targets the default branch is merged.
  const upper = { ...goodPr, base: { ref: "claude/part-1" } };
  const { github, calls } = fakeGitHub({ pr: upper });
  const result = await run({ github, context: context(), core }, fast);
  assert.equal(result.merged, false);
  assert.equal(calls.merge.length, 0);
  assert.match(calls.comments[0].body, /sign off the pull request below this one first/);
});

test("a non-owner comment does nothing at all", async () => {
  const { github, calls } = fakeGitHub();
  const result = await run({ github, context: context(payload({ comment: { user: { login: "mallory" } } })), core }, fast);
  assert.equal(result.merged, false);
  assert.equal(calls.merge.length + calls.comments.length, 0);
});

test("commits pushed after the comment block the merge", async () => {
  const { github, calls } = fakeGitHub({ headCommitDate: "2026-11-08T10:05:00Z" });
  const result = await run({ github, context: context(), core }, fast);
  assert.match(result.reason, /new commits arrived/);
  assert.equal(calls.merge.length, 0);
  assert.equal(calls.comments.length, 1);
});

test("a PR with failing checks is refused with an explanation", async () => {
  const { github, calls } = fakeGitHub({ pr: { ...goodPr, mergeable_state: "blocked" } });
  const result = await run({ github, context: context(), core }, fast);
  assert.equal(result.merged, false);
  assert.equal(calls.merge.length, 0);
  assert.match(calls.comments[0].body, /Not merged/);
});

test("if GitHub refuses the merge, the owner is told why", async () => {
  const error = Object.assign(new Error("Bad request"), {
    status: 400,
    response: { data: { status: "failed", details: { message: "Head branch was modified" } } },
  });
  const { github, calls } = fakeGitHub({ mergeError: error });
  const result = await run({ github, context: context(), core }, fast);
  assert.equal(result.merged, false);
  assert.match(calls.comments[0].body, /Head branch was modified/);
  assert.equal(calls.reactions.length, 0);
});

test("a second /signoff while a merge is pending is refused", async () => {
  const error = Object.assign(new Error("Conflict"), {
    status: 409,
    response: { data: { status: "pending", details: { message: "already requested", uuid: "u" } } },
  });
  const { github, calls } = fakeGitHub({ mergeError: error });
  await run({ github, context: context(), core }, fast);
  assert.match(calls.comments[0].body, /already in progress/);
});
