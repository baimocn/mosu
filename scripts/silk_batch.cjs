/**
 * 批量把微信 SILK_V3 语音转成可在浏览器播放的音频。
 *
 * 用法: node scripts/silk_batch.cjs <manifest.json>
 * manifest: [{ id, silk: "输入.silk", out: "输出.mp3", rate: 24000 }, ...]
 *
 * 链路: silk-wasm 解码 -> PCM(s16le) -> ffmpeg 转 mp3
 * 只在 ffmpeg 可用时产出 mp3；否则直接写 PCM（浏览器放不了，但数据不丢）。
 */
const fs = require('fs');
const path = require('path');
const os = require('os');
const { execFileSync } = require('child_process');
const silk = require('silk-wasm');

// ffmpeg 查找顺序：FFMPEG 环境变量 > PATH（which）> 常见安装位置；找不到则只产 PCM
function findFfmpeg() {
  if (process.env.FFMPEG) return process.env.FFMPEG;
  const { execSync } = require('child_process');
  const candidates = process.platform === 'win32'
    ? ['ffmpeg', 'ffmpeg.exe',
       'C:\\Program Files\\ffmpeg\\bin\\ffmpeg.exe',
       `${process.env.LOCALAPPDATA || ''}\\Microsoft\\WinGet\\Links\\ffmpeg.exe`]
    : ['ffmpeg', '/usr/bin/ffmpeg', '/usr/local/bin/ffmpeg'];
  for (const c of candidates) {
    try { execSync(`"${c}" -version`, { stdio: 'ignore' }); return c; } catch {}
  }
  return null;
}
const FFMPEG = findFfmpeg();

function hasFfmpeg() {
  try {
    execFileSync(FFMPEG, ['-version'], { stdio: 'ignore' });
    return true;
  } catch { return false; }
}

(async () => {
  const manifestPath = process.argv[2];
  if (!manifestPath) { console.error('用法: node silk_batch.cjs <manifest.json>'); process.exit(1); }
  const items = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  const useFf = hasFfmpeg();
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'silk_'));
  let ok = 0, fail = 0;

  for (const it of items) {
    try {
      const buf = fs.readFileSync(it.silk);
      const res = await silk.decode(buf, it.rate || 24000);
      if (useFf) {
        const pcm = path.join(tmpDir, it.id + '.pcm');
        fs.writeFileSync(pcm, Buffer.from(res.data.buffer));
        fs.mkdirSync(path.dirname(it.out), { recursive: true });
        execFileSync(FFMPEG, ['-y', '-f', 's16le', '-ar', String(it.rate || 24000),
          '-ac', '1', '-i', pcm, '-b:a', '32k', it.out], { stdio: 'ignore' });
        try { fs.unlinkSync(pcm); } catch {}   // 删不掉就留着（系统临时目录，无关紧要）
      } else {
        fs.mkdirSync(path.dirname(it.out), { recursive: true });
        fs.writeFileSync(it.out, Buffer.from(res.data.buffer));
      }
      ok++;
    } catch (e) {
      fail++;
      console.error(`  [!] ${it.id}: ${String(e.message).slice(0, 80)}`);
    }
  }
  try { fs.rmSync(tmpDir, { recursive: true, force: true }); } catch {}
  console.log(`[✓] silk 转码完成: 成功 ${ok}, 失败 ${fail}`);
})();
