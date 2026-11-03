// Generates src/api/types.ts from the committed backend contract
// (../master_backend/app/api/schema.json) with json-schema-to-typescript.
//
// `node scripts/gen-api.mjs`         — write src/api/types.ts (npm run gen:api)
// `node scripts/gen-api.mjs --check` — regenerate to a temp file and fail on any
//                                      difference (npm run check:api)
//
// The Pydantic models behind schema.json are the single source of truth; nobody
// hand-writes API types in this repo (API.md).
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomBytes } from 'node:crypto';
import { compileFromFile } from 'json-schema-to-typescript';

const here = dirname(fileURLToPath(import.meta.url));
const dashboardRoot = resolve(here, '..');
const schemaPath = resolve(dashboardRoot, '..', 'master_backend', 'app', 'api', 'schema.json');
const outPath = join(dashboardRoot, 'src', 'api', 'types.ts');
const check = process.argv.includes('--check');

const OPTIONS = {
  bannerComment: [
    '/* eslint-disable */',
    '/**',
    ' * GENERATED from master_backend/app/api/schema.json — DO NOT EDIT BY HAND.',
    ' * Regenerate: `npm run gen:api`. `npm run check:api` fails when this file is stale.',
    ' */',
  ].join('\n'),
  // The schema root only carries $defs (each message/model is a definition); export
  // every one of them, referenced from the root or not.
  unreachableDefinitions: true,
  declareExternallyAs: 'interface',
  sortJsonKeys: false,
  style: { singleQuote: true, semi: true },
  cwd: dirname(schemaPath),
};

const generated = await compileFromFile(schemaPath, OPTIONS);

if (!check) {
  mkdirSync(dirname(outPath), { recursive: true });
  writeFileSync(outPath, generated, 'utf8');
  console.log(`wrote ${outPath}`);
} else {
  const tmp = join(tmpdir(), `types-${randomBytes(4).toString('hex')}.ts`);
  writeFileSync(tmp, generated, 'utf8');
  let current = '';
  try {
    current = readFileSync(outPath, 'utf8');
  } catch {
    current = '';
  }
  if (current !== generated) {
    console.error(
      'src/api/types.ts is stale: it differs from what master_backend/app/api/schema.json generates.\n' +
        'Run `npm run gen:api` in dashboard/ and commit the result.',
    );
    process.exitCode = 1;
  } else {
    console.log('src/api/types.ts matches schema.json');
  }
}
