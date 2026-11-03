import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createCrowdServer } from './service.mjs';

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
try { process.loadEnvFile(path.join(repo, '.env')); }
catch (error) { if (error.code !== 'ENOENT') throw error; }
// Private workspace convenience, matching the DJ bridge. Standalone installs
// use their own environment or repo .env; never copy credentials into artifacts.
if (!process.env.MODEL_API_KEY) {
  try { process.loadEnvFile(path.resolve(repo, '../experiments/meta-dj-agent/.env')); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
}
const storageRoot = path.resolve(process.env.CROWD_ARTIFACTS_DIR || path.join(repo, 'artifacts/crowd'));
const server = createCrowdServer({storageRoot, key: process.env.MODEL_API_KEY});
server.listen(8767, '127.0.0.1', () => console.log('Crowd observation service: http://127.0.0.1:8767 (uploaded clips and webcam; vision only)'));
