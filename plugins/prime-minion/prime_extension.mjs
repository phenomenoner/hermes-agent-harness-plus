const LOOPBACK_PROXY = /^http:\/\/127\.0\.0\.1:\d+\/v1$/;

export default function registerHermesCodexBridge(pi) {
	const baseUrl = process.env.HERMES_MINION_PROXY_BASE_URL;
	const apiKey = process.env.HERMES_MINION_PROXY_API_KEY;
	if (!baseUrl || !LOOPBACK_PROXY.test(baseUrl)) {
		throw new Error("HERMES_MINION_PROXY_BASE_URL must be a loopback /v1 URL");
	}
	if (!apiKey) {
		throw new Error("HERMES_MINION_PROXY_API_KEY is required");
	}

	// Keep Prime's native Codex stream/tool-call implementation and model catalog;
	// override only request routing and the non-secret synthetic bearer.
	pi.registerProvider("openai-codex", {
		baseUrl,
		apiKey,
		headers: { "X-Hermes-Prime-Minion": "1" },
	});
}
