// Cloudflare Worker that starts the GitHub Actions monitor every five minutes.
// GitHub's own cron is often delayed by 10-30+ minutes, so this external trigger
// calls the workflow_dispatch API on a reliable schedule instead.
export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env));
  },
  // Visiting the worker URL triggers one run manually (handy for testing).
  async fetch(request, env) {
    const status = await dispatch(env);
    return new Response(`GitHub responded ${status}\n`, { status: status === 204 ? 200 : 502 });
  },
};

async function dispatch(env) {
  const url = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/actions/workflows/${env.WORKFLOW_FILE}/dispatches`;
  const response = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "apm-notifier-dispatcher",
    },
    body: JSON.stringify({ ref: env.GITHUB_REF || "main" }),
  });
  if (response.status !== 204) {
    console.error(`Dispatch failed: ${response.status} ${await response.text()}`);
  }
  return response.status;
}
