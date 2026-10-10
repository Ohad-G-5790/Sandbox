#!/usr/bin/env node
// Builds dist/artifact.html: the page in the body-only form that claude.ai Artifacts expect
// (title and styles first, no html/head/body tags). Scripts stay as separate files.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const pianoDir = join(dirname(fileURLToPath(import.meta.url)), "..");
const html = readFileSync(join(pianoDir, "index.html"), "utf8");
const css = readFileSync(join(pianoDir, "style.css"), "utf8");
const title = html.match(/<title>([^<]*)<\/title>/)[1];
const body = html.match(/<body>([\s\S]*)<\/body>/)[1].trim();
const out = `<title>${title}</title>\n<style>\n${css}\n</style>\n${body}\n`;
mkdirSync(join(pianoDir, "dist"), { recursive: true });
writeFileSync(join(pianoDir, "dist", "artifact.html"), out);
console.log(`dist/artifact.html: ${(out.length / 1024).toFixed(1)} KB, title "${title}"`);
