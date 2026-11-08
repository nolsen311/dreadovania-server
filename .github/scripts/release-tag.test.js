// SPDX-License-Identifier: GPL-3.0-or-later
// Tests for release-tag.js. Run: node --test .github/scripts/
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  run, checkTrigger, checkReleasable, readVersion, checkVersion, checkRuns, compareVersions,
} = require("./release-tag.js");

const OWNER = "nolsen311";

function payload(overrides = {}) {
  return {
    issue: { number: 21, pull_request: { url: "x" } },
    comment: {
      id: 77,
      body: "/release",
      user: { login: OWNER },
      author_association: "OWNER",
      ...overrides.comment,
    },
    ...overrides.top,
  };
}

const mergedPr = {
  number: 21,
  merged: true,
  base: { ref: "main" },
  head: { sha: "head123" },
  merge_commit_sha: "merge456",
};

// ---- trigger and readiness ----------------------------------------------------------------

test("only an exact /release comment by the owner on a PR triggers", () => {
  assert.equal(checkTrigger(payload(), OWNER), null);
  assert.equal(checkTrigger(payload({ comment: { body: " /release\n" } }), OWNER), null);
  assert.match(checkTrigger(payload({ comment: { body: "/release v9.9.9" } }), OWNER), /not a \/release/);
  assert.match(checkTrigger(payload({ comment: { body: "/signoff" } }), OWNER), /not a \/release/);
  assert.match(checkTrigger(payload({ comment: { user: { login: "mallory" } } }), OWNER), /owner/);
  assert.match(checkTrigger(payload({ comment: { author_association: "MEMBER" } }), OWNER), /owner/);
  assert.match(checkTrigger({ ...payload(), issue: { number: 1 } }, OWNER), /not a pull request/);
});

test("only a PR merged into the default branch can be released", () => {
  assert.equal(checkReleasable(mergedPr, "main"), null);
  assert.match(checkReleasable({ ...mergedPr, merged: false }, "main"), /\/signoff first/);
  assert.match(checkReleasable({ ...mergedPr, base: { ref: "dev" } }, "main"), /into main/);
  assert.match(checkReleasable({ ...mergedPr, merge_commit_sha: null }, "main"), /merge commit/);
});

// ---- versions ------------------------------------------------------------------------------

test("versions are read from common version files", () => {
  const pyproject = '[build-system]\nversion = "9.9.9"\n\n[project]\nname = "x"\nversion = "0.2.0"\n';
  assert.equal(readVersion(pyproject, "pyproject.toml"), "0.2.0");
  assert.equal(readVersion('[package]\nname = "x"\nversion = "1.4.0"\n', "Cargo.toml"), "1.4.0");
  assert.equal(readVersion('{"name": "x", "version": "3.0.1"}', "package.json"), "3.0.1");
  assert.equal(readVersion("2.5.0\n", "VERSION"), "2.5.0");
  assert.throws(() => readVersion('[project]\nversion = "1.0"\n', "pyproject.toml"), /like 1\.2\.3/);
  assert.throws(() => readVersion("[project]\nname = 'x'\n", "pyproject.toml"), /like 1\.2\.3/);
  assert.throws(() => readVersion("1.0.0; rm -rf /", "VERSION"), /like 1\.2\.3/);
});

test("a version must be new and newer than every release", () => {
  const tags = ["v0.1.0", "v0.2.0", "dev-build", "v0.10.0"];
  assert.equal(checkVersion("0.10.1", tags), null);
  assert.equal(checkVersion("1.0.0", []), null);
  assert.match(checkVersion("0.2.0", tags), /already exists/);
  assert.match(checkVersion("0.9.0", tags), /isn't newer than the latest release, v0\.10\.0/);
  assert.ok(compareVersions("0.10.0", "0.9.9") > 0); // numeric, not text, comparison
});

test("the tested commit's checks must all have passed", () => {
  const ok = { status: "completed", conclusion: "success", name: "Test" };
  assert.equal(checkRuns([ok, { ...ok, name: "DCO audit", conclusion: "skipped" }]), null);
  assert.match(checkRuns([]), /no checks/);
  assert.match(checkRuns([ok, { name: "Lint", status: "in_progress" }]), /still running \(Lint\)/);
  assert.match(checkRuns([ok, { ...ok, name: "Lint", conclusion: "failure" }]), /didn't pass \(Lint\)/);
});

// ---- the whole flow, against a fake GitHub API ---------------------------------------------

function fakeGitHub({
  pr = mergedPr,
  version = "0.1.1",
  tags = ["v0.1.0"],
  runs = [{ name: "Test", status: "completed", conclusion: "success" }],
  comparison = "ahead",
  refError,
  dispatchError,
} = {}) {
  const calls = { tags: [], refs: [], dispatches: [], comments: [], reactions: [], contentRefs: [], checkRefs: [] };
  const pyproject = `[project]\nname = "dreadovania"\nversion = "${version}"\n`;
  const github = {
    paginate: async (fn, args) => fn(args),
    rest: {
      pulls: { get: async () => ({ data: pr }) },
      repos: {
        get: async () => ({ data: { default_branch: "main" } }),
        compareCommitsWithBasehead: async () => ({ data: { status: comparison } }),
        getContent: async (args) => {
          calls.contentRefs.push(args.ref);
          return { data: { content: Buffer.from(pyproject).toString("base64") } };
        },
        listTags: () => tags.map((name) => ({ name })),
      },
      checks: {
        listForRef: (args) => {
          calls.checkRefs.push(args.ref);
          return runs;
        },
      },
      git: {
        createTag: async (args) => {
          calls.tags.push(args);
          return { data: { sha: "tagobj789" } };
        },
        createRef: async (args) => {
          if (refError) throw new Error(refError);
          calls.refs.push(args);
          return { data: {} };
        },
      },
      actions: {
        createWorkflowDispatch: async (args) => {
          if (dispatchError) throw new Error(dispatchError);
          calls.dispatches.push(args);
        },
      },
      issues: { createComment: async (args) => calls.comments.push(args) },
      reactions: { createForIssueComment: async (args) => calls.reactions.push(args) },
    },
  };
  return { github, calls };
}

const core = { notice: () => {} };
const context = (p = payload()) => ({ payload: p, repo: { owner: OWNER, repo: "dreadovania-server" } });
const opts = { versionFile: "pyproject.toml", releaseWorkflow: "release.yml" };

test("a valid /release tags the merge commit and starts the release on the tag", async () => {
  const { github, calls } = fakeGitHub();
  const result = await run({ github, context: context(), core }, opts);
  assert.deepEqual(result, { released: true, tag: "v0.1.1", sha: "merge456" });
  assert.equal(calls.contentRefs[0], "merge456"); // the version at the merge commit
  assert.equal(calls.checkRefs[0], "head123"); // the commit CI tested
  assert.equal(calls.tags[0].object, "merge456");
  assert.equal(calls.tags[0].tag, "v0.1.1");
  assert.match(calls.tags[0].message, /Released by @nolsen311 with \/release on #21/);
  assert.deepEqual(calls.refs[0], { owner: OWNER, repo: "dreadovania-server", ref: "refs/tags/v0.1.1", sha: "tagobj789" });
  assert.deepEqual(calls.dispatches[0], { owner: OWNER, repo: "dreadovania-server", workflow_id: "release.yml", ref: "v0.1.1" });
  assert.equal(calls.reactions[0].content, "+1");
  assert.match(calls.comments[0].body, /Tagged v0\.1\.1 at merge456\. The release workflow is starting/);
});

test("a non-owner comment does nothing at all", async () => {
  const { github, calls } = fakeGitHub();
  const result = await run({ github, context: context(payload({ comment: { user: { login: "mallory" } } })), core }, opts);
  assert.equal(result.released, false);
  assert.equal(calls.tags.length + calls.comments.length + calls.dispatches.length, 0);
});

for (const [name, setup, reason] of [
  ["an unmerged PR", { pr: { ...mergedPr, merged: false } }, /\/signoff first/],
  ["a merge commit no longer on main", { comparison: "diverged" }, /isn't on main/],
  ["failing checks", { runs: [{ name: "Test", status: "completed", conclusion: "failure" }] }, /didn't pass/],
  ["a version that's already released", { version: "0.1.0" }, /already exists/],
  ["an older version", { version: "0.0.9" }, /isn't newer/],
  ["a malformed version", { version: "0.1" }, /like 1\.2\.3/],
]) {
  test(`${name} is refused with an explanation, and nothing is tagged`, async () => {
    const { github, calls } = fakeGitHub(setup);
    const result = await run({ github, context: context(), core }, opts);
    assert.equal(result.released, false);
    assert.match(result.reason, reason);
    assert.equal(calls.refs.length + calls.dispatches.length + calls.reactions.length, 0);
    assert.match(calls.comments[0].body, /^Not released:/);
  });
}

test("if the ruleset blocks the tag, the owner is told what to check", async () => {
  const { github, calls } = fakeGitHub({ refError: "Repository rule violations found" });
  const result = await run({ github, context: context(), core }, opts);
  assert.equal(result.released, false);
  assert.match(calls.comments[0].body, /tag ruleset lets GitHub Actions create version tags/);
  assert.equal(calls.dispatches.length, 0);
});

test("if the release can't be started, the tag stays and the owner is told how to start it", async () => {
  const { github, calls } = fakeGitHub({ dispatchError: "Workflow does not have 'workflow_dispatch' trigger" });
  const result = await run({ github, context: context(), core }, opts);
  assert.equal(result.tagged, "v0.1.1");
  assert.match(calls.comments[0].body, /Tagged v0\.1\.1 .* couldn't be started .* Actions tab/);
  assert.equal(calls.reactions.length, 0);
});

test("without a release workflow, it only tags", async () => {
  const { github, calls } = fakeGitHub();
  const result = await run({ github, context: context(), core }, { versionFile: "pyproject.toml" });
  assert.equal(result.released, true);
  assert.equal(calls.dispatches.length, 0);
  assert.equal(calls.comments[0].body.split("\n")[0], "Tagged v0.1.1 at merge456.");
});
