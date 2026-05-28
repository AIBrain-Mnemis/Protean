/**
 * Linear-interpolation resampling for int16 LE mono PCM.
 *
 * Adequate for voice traffic: TRTC remote audio comes in at 48 kHz mono,
 * Gemini wants 16 kHz mono, ratio 1:3 — linear interp is indistinguishable
 * from a band-limited resample on speech here. If we ever push music we'd
 * need a windowed sinc, but that's out of scope.
 *
 * Pass-through when fromRate === toRate to avoid allocations on the hot path.
 */
export function resampleInt16Mono(
  input: Int16Array,
  fromRate: number,
  toRate: number,
): Int16Array {
  if (fromRate === toRate || input.length === 0) return input;
  const ratio = toRate / fromRate;
  const newLen = Math.max(1, Math.floor(input.length * ratio));
  const out = new Int16Array(newLen);
  const lastIdx = input.length - 1;
  for (let i = 0; i < newLen; i += 1) {
    const t = (i * lastIdx) / (newLen - 1 || 1);
    const i0 = Math.floor(t);
    const i1 = Math.min(lastIdx, i0 + 1);
    const frac = t - i0;
    const a = input[i0] ?? 0;
    const b = input[i1] ?? 0;
    out[i] = Math.round(a + (b - a) * frac);
  }
  return out;
}
