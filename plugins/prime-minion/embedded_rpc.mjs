#!/usr/bin/env node

import { isAbsolute, join } from "node:path";
import { pathToFileURL } from "node:url";

const runtimeRoot = process.env.HERMES_PRIME_MINION_RUNTIME_ROOT;
if (!runtimeRoot || !isAbsolute(runtimeRoot)) {
	throw new Error("HERMES_PRIME_MINION_RUNTIME_ROOT must be an absolute path");
}

const mainUrl = pathToFileURL(
	join(runtimeRoot, "packages", "coding-agent", "src", "main.ts"),
).href;
const extensionUrl = new URL("./prime_extension.mjs", import.meta.url).href;
const [{ main }, { default: registerHermesCodexBridge }] = await Promise.all([
	import(mainUrl),
	import(extensionUrl),
]);

await main(process.argv.slice(2), {
	extensionFactories: [registerHermesCodexBridge],
});
