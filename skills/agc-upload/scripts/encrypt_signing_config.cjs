// Convert a private stdin password into the selected Hvigor SDK's material format.
// stdout is consumed privately by provision_release.py, never displayed to users.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
function writePrivate(file, data) { fs.writeFileSync(file, data, {mode: 0o600, flag: 'wx'}); }
function seal(key, data) {
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-128-gcm', key, iv);
  const body = Buffer.concat([cipher.update(data), cipher.final(), cipher.getAuthTag()]);
  const size = Buffer.alloc(4); size.writeUInt32BE(body.length);
  return Buffer.concat([size, iv, body]);
}
try {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const store = path.resolve(input.store_dir);
  if (typeof input.password !== 'string' || input.password.length < 8) throw new Error();
  let studio = process.env.DEVECO_PATH || '/Applications/DevEco-Studio.app';
  if (studio.endsWith('.app')) studio = path.join(studio, 'Contents');
  // DevEco normally supplies this sibling-package alias through its launcher.
  const Module = require('node:module');
  const resolve = Module._resolveFilename;
  let DecipherUtil;
  try {
    Module._resolveFilename = function(request, parent, isMain, options) {
      return resolve.call(this, request === '@ohos/hvigor' ? path.join(studio, 'tools/hvigor/hvigor') : request, parent, isMain, options);
    };
    ({DecipherUtil} = require(path.join(studio, 'tools/hvigor/hvigor-ohos-plugin/src/utils/decipher-util.js')));
  } finally { Module._resolveFilename = resolve; }
  const material = path.join(store, 'material');
  fs.mkdirSync(store, {recursive: true, mode: 0o700});
  if (!fs.existsSync(material)) {
    const temporary = fs.mkdtempSync(path.join(store, '.material-'));
    fs.chmodSync(temporary, 0o700);
    try {
      const [fd, ac, ce] = DecipherUtil.dirs;
      for (const name of [fd, ac, ce]) fs.mkdirSync(path.join(temporary, name), {mode: 0o700});
      const parts = [0,1,2].map(index => {
        const bytes = crypto.randomBytes(16);
        const dir = path.join(temporary, fd, String(index));
        fs.mkdirSync(dir, {mode: 0o700}); writePrivate(path.join(dir, 'data'), bytes);
        return new Int8Array(bytes);
      });
      const salt = crypto.randomBytes(16);
      const root = DecipherUtil.getRootKey(parts, new Int8Array(salt), 'private signing configuration');
      const work = crypto.randomBytes(16);
      writePrivate(path.join(temporary, ac, 'data'), salt);
      writePrivate(path.join(temporary, ce, 'data'), seal(root, work));
      fs.renameSync(temporary, material);
    } finally { if (fs.existsSync(temporary)) fs.rmSync(temporary, {recursive: true}); }
  }
  const key = DecipherUtil.getKey(store, 'private signing configuration');
  const encrypted = seal(key, Buffer.from(input.password)).toString('hex');
  if (DecipherUtil.decryptPwd(store, encrypted, 'private signing configuration') !== input.password) throw new Error();
  process.stdout.write(JSON.stringify({encrypted_password: encrypted}));
} catch (_) {
  process.stderr.write('Unable to prepare SDK signing-password material; sensitive diagnostics suppressed.\n');
  process.exitCode = 2;
}
