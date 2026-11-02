export type BuildResult = {ok: boolean; error?: string; buildId?: string};

// Only explicit song counts affect the numeric control; BPM and durations do not.
export function requestedTrackCount(prompt: string): number | null {
  const digits = prompt.match(/(?<![\d.\-])(\d{1,3})\s*(?:首|tracks?\b|songs?\b)/i);
  if (digits) return Number(digits[1]);
  const chinese = prompt.match(/([一二兩两三四五六七八九十]{1,3})\s*首/);
  if (chinese) {
    const n: Record<string, number> = {一:1,二:2,兩:2,两:2,三:3,四:4,五:5,六:6,七:7,八:8,九:9};
    const text = chinese[1];
    if (text.includes("十")) {
      const [tens, ones] = text.split("十");
      return (tens ? n[tens] : 1) * 10 + (ones ? n[ones] : 0);
    }
    return n[text] ?? null;
  }
  const words = ["two","three","four","five","six","seven","eight","nine","ten","eleven","twelve"];
  const word = prompt.toLowerCase().match(/\b(two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)[ -]+(?:tracks?|songs?)\b/);
  return word ? words.indexOf(word[1]) + 2 : null;
}
