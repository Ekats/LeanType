import com.android.inputmethod.latin.BinaryDictionary;
import com.android.inputmethod.latin.DicTraverseSession;
import com.android.inputmethod.keyboard.ProximityInfo;
import java.io.File;
import java.util.*;

/**
 * Host-side probe of LeanType's native LatinIME engine (the same C++ code the app runs), used to
 * inspect and validate dictionaries without a phone: real typed-word scores, real next-word predictions.
 * Modes: demo | pred ctx... | type word... | nscore word... | sim +typo -keep... | check ctx=a|b ... typed:=want ...
 */
public class Probe {
    static final int MAXW = 48, MAXR = 18;
    static final String[] ROWS = {"qwertyuiop", "asdfghjkl", "zxcvbnm"};
    static final int KW = 100, KH = 150, W = 1000, H = 450, GW = 32, GH = 16;
    static Map<Character, int[]> keyPos = new HashMap<>();
    static long prox;

    static long buildProximity() {
        List<int[]> keys = new ArrayList<>(); // x,y,w,h,code
        for (int r = 0; r < 3; r++) {
            int off = r == 0 ? 0 : (r == 1 ? 50 : 150);
            for (int i = 0; i < ROWS[r].length(); i++) {
                char c = ROWS[r].charAt(i);
                int x = off + i * KW, y = r * KH;
                keys.add(new int[]{x, y, KW, KH, c});
                keyPos.put(c, new int[]{x + KW / 2, y + KH / 2});
            }
        }
        int cw = (W + GW - 1) / GW, ch = (H + GH - 1) / GH;
        int[] proxArr = new int[GW * GH * 16];
        Arrays.fill(proxArr, -1);
        double th = KW * 1.2, th2 = th * th;
        for (int gy = 0; gy < GH; gy++) for (int gx = 0; gx < GW; gx++) {
            int cx = gx * cw + cw / 2, cy = gy * ch + ch / 2, idx = (gy * GW + gx) * 16, n = 0;
            for (int[] k : keys) {
                int dx = Math.max(0, Math.max(k[0] - cx, cx - (k[0] + k[2])));
                int dy = Math.max(0, Math.max(k[1] - cy, cy - (k[1] + k[3])));
                if (dx * dx + dy * dy < th2 && n < 16) proxArr[idx + n++] = k[4];
            }
        }
        int n = keys.size();
        int[] kx = new int[n], ky = new int[n], kw = new int[n], kh = new int[n], codes = new int[n];
        for (int i = 0; i < n; i++) { int[] k = keys.get(i); kx[i]=k[0]; ky[i]=k[1]; kw[i]=k[2]; kh[i]=k[3]; codes[i]=k[4]; }
        return ProximityInfo.setProximityInfoNative(W, H, GW, GH, KW, KH, proxArr, n, kx, ky, kw, kh, codes, null, null, null);
    }

    static int[] cps(String s) { return s.codePoints().toArray(); }

    static List<String[]> query(long dict, long session, String typed, String... prev) {
        int[][] prevArr = new int[3][];
        boolean[] bos = new boolean[3];
        int pc = 0;
        // prev given most-recent-first
        for (String p : prev) { if (p.equals("<s>")) { bos[pc] = true; prevArr[pc] = new int[0]; } else prevArr[pc] = cps(p); pc++; }
        if (pc == 0) { bos[0] = true; prevArr[0] = new int[0]; pc = 1; }
        int n = typed.length();
        int[] x = new int[MAXW], y = new int[MAXW], t = new int[MAXW], pid = new int[MAXW], in = new int[MAXW];
        for (int i = 0; i < n; i++) { int[] p = keyPos.get(Character.toLowerCase(typed.charAt(i))); x[i]=p[0]; y[i]=p[1]; t[i]=i*150; in[i]=typed.charAt(i); }
        int[] opts = {0, 0, 1, 0, 1000};
        int[] outCount = new int[1], outCp = new int[MAXW * MAXR], outScores = new int[MAXR], outIdx = new int[MAXR], outTypes = new int[MAXR], conf = new int[1];
        float[] wt = {-1f};
        DicTraverseSession.initDicTraverseSessionNative(session, dict, null, 0);
        BinaryDictionary.getSuggestionsNative(dict, prox, session, x, y, t, pid, in, n, opts, prevArr, bos, pc, outCount, outCp, outScores, outIdx, outTypes, conf, wt);
        List<String[]> res = new ArrayList<>();
        for (int i = 0; i < outCount[0]; i++) {
            StringBuilder sb = new StringBuilder();
            for (int j = 0; j < MAXW; j++) { int c = outCp[i * MAXW + j]; if (c == 0) break; sb.appendCodePoint(c); }
            res.add(new String[]{sb.toString(), String.valueOf(outScores[i])});
        }
        res.sort((a, b) -> Integer.parseInt(b[1]) - Integer.parseInt(a[1]));
        return res;
    }

    static String fmt(List<String[]> r, int k) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < Math.min(k, r.size()); i++) sb.append(String.format("%s(%s) ", r.get(i)[0], r.get(i)[1]));
        return r.isEmpty() ? "<none>" : sb.toString();
    }

    static String mark(boolean corrected, boolean want) { return (corrected ? "fix" : "keep") + (corrected == want ? " ✓" : " ✗"); }

    public static void main(String[] a) throws Exception {
        System.load(new File(System.getProperty("latinime.lib", "libjni_latinime.so")).getAbsolutePath());
        String path = a[0];
        long dict = BinaryDictionary.openNative(path, 0, new File(path).length(), false);
        long session = DicTraverseSession.setDicTraverseSessionNative(a.length > 1 ? a[1] : "en_US", new File(path).length());
        prox = buildProximity();
        String mode = a.length > 2 ? a[2] : "demo";
        if (mode.equals("demo")) {
            System.out.println("== TYPED-WORD suggestions (score scale) ==");
            for (String w : new String[]{"the", "teh", "hte", "yhe", "fo", "adn", "thw", "becuase", "recieve", "definately", "tommorow", "goint", "hekko", "wnat", "sone", "infrastructurr"})
                System.out.printf("%-15s %s%n", w, fmt(query(dict, session, w, "<s>"), 5));
            System.out.println("\n== NEXT-WORD predictions (score scale) ==");
            for (String[] ctx : new String[][]{{"<s>"}, {"I"}, {"i"}, {"thank"}, {"how"}, {"going", "am"}, {"see", "to"}, {"the"}, {"of"}, {"meeting"}, {"tomorrow"}, {"Lisbon"}, {"Fedora"}, {"keyboard"}})
                System.out.printf("%-18s %s%n", String.join("<-", ctx), fmt(query(dict, session, "", ctx), 8));
        } else if (mode.equals("coverage")) {
            // a[3] = file with one word per line: fraction of words that have ANY bigram continuation
            List<String> words = java.nio.file.Files.readAllLines(java.nio.file.Paths.get(a[3]));
            int has = 0, total = 0;
            for (String w : words) { if (w.isBlank()) continue; total++; if (!query(dict, session, "", w.trim()).isEmpty()) has++; }
            System.out.printf("words with >=1 next-word prediction: %d / %d (%.1f%%)%n", has, total, 100.0 * has / total);
        } else if (mode.equals("pred")) {
            for (int i = 3; i < a.length; i++) System.out.printf("%-18s %s%n", a[i], fmt(query(dict, session, "", a[i].split("<-")), 10));
        } else if (mode.equals("nscore")) {
            System.out.printf("%-12s %-14s %9s %7s | %-12s %9s%n", "typed", "top", "score", "norm", "2nd", "score");
            for (int i = 3; i < a.length; i++) {
                List<String[]> r = query(dict, session, a[i], "<s>");
                // drop exact typed word from candidates, as Suggest.kt does
                String typed = a[i];
                String[] inDict = r.stream().filter(x -> x[0].equals(typed)).findFirst().orElse(null);
                r.removeIf(x -> x[0].equals(typed));
                if (r.isEmpty()) { System.out.printf("%-12s <none>%n", typed); continue; }
                String[] top = r.get(0);
                float ns = com.android.inputmethod.latin.utils.BinaryDictionaryUtils.calcNormalizedScoreNative(cps(typed), cps(top[0]), Integer.parseInt(top[1]));
                String[] sec = r.size() > 1 ? r.get(1) : new String[]{"-", "0"};
                System.out.printf("%-12s %-14s %9s %7.3f | %-12s %9s %s%n", typed, top[0], top[1], ns, sec[0], sec[1], inDict != null ? "  [typed word in dict: " + inDict[1] + "]" : "");
            }
        } else if (mode.equals("sim")) {
            // a[3..]: words prefixed with + (should be corrected) or - (should be left alone)
            int[] good = new int[3]; int n = 0;
            String[] names = {"LeanType old (Balanced 0.067)", "LeanType patched (Balanced 0.185)", "HeliBoard upstream (~0.188)"};
            System.out.printf("%-12s %-14s %6s | %-8s %-8s %-8s%n", "typed", "top", "norm", "old", "patched", "upstream");
            for (int i = 3; i < a.length; i++) {
                boolean want = a[i].charAt(0) == '+'; String w = a[i].substring(1);
                List<String[]> r = query(dict, session, w, "<s>");
                boolean inDict = r.stream().anyMatch(x -> x[0].equals(w));
                r.removeIf(x -> x[0].equalsIgnoreCase(w));
                if (inDict) { System.out.printf("%-12s (in dictionary, skipped)%n", w); continue; }
                String top = r.isEmpty() ? "-" : r.get(0)[0]; int sc = r.isEmpty() ? 0 : Integer.parseInt(r.get(0)[1]);
                int sc2 = r.size() > 1 ? Integer.parseInt(r.get(1)[1]) : 0;
                float ns = r.isEmpty() ? 0 : com.android.inputmethod.latin.utils.BinaryDictionaryUtils.calcNormalizedScoreNative(cps(w), cps(top), sc);
                boolean caseOnly = top.equalsIgnoreCase(w);
                // old LeanType
                boolean old;
                if (r.isEmpty() || caseOnly) old = false;
                else if (w.length() <= 3) old = false;
                else if (ns >= 0.067f) old = true;
                else old = w.length() > 3 && sc > 200000 && ns >= 0.067f * (3f / Math.min(w.length(), 15));
                // patched
                boolean nw;
                if (r.isEmpty() || caseOnly) nw = false;
                else if (w.length() <= 3) nw = ns >= 0.25f && sc >= sc2 * 1.1f;
                else nw = ns >= 0.185f;
                // upstream HeliBoard: threshold 0.5-0.5*0.24^0.33
                float up = (float)(0.5 - 0.5 * Math.pow(0.24, 0.33));
                boolean ups = !r.isEmpty() && !caseOnly && ns >= up;
                boolean[] res = {old, nw, ups};
                for (int k = 0; k < 3; k++) if (res[k] == want) good[k]++;
                n++;
                System.out.printf("%-12s %-14s %6.3f | %-8s %-8s %-8s %s%n", w, top, ns, mark(old, want), mark(nw, want), mark(ups, want), want ? "(typo)" : "(keep)");
            }
            for (int k = 0; k < 3; k++) System.out.printf("%s: %d/%d right%n", names[k], good[k], n);
        } else if (mode.equals("check")) {
            // "ctx=exp1|exp2": one of the expected words must be in the top 5 predictions after ctx
            // "typed:=exp": the best non-typed suggestion for typed must be exp
            int fails = 0;
            for (int i = 3; i < a.length; i++) {
                boolean typing = a[i].contains(":=");
                String[] kv = a[i].split(typing ? ":=" : "=", 2);
                List<String> want = Arrays.asList(kv[1].split("\\|"));
                List<String[]> r = typing ? query(dict, session, kv[0], "<s>") : query(dict, session, "", kv[0].split("<-"));
                if (typing) r.removeIf(x -> x[0].equals(kv[0]));
                List<String> top = new ArrayList<>();
                for (int k = 0; k < Math.min(typing ? 1 : 5, r.size()); k++) top.add(r.get(k)[0]);
                boolean ok = top.stream().anyMatch(want::contains);
                if (!ok) fails++;
                System.out.printf("%s %-14s -> %s%n", ok ? "PASS" : "FAIL", kv[0], fmt(r, 8));
            }
            System.out.println(fails == 0 ? "all checks passed" : fails + " check(s) failed");
            System.exit(fails == 0 ? 0 : 1);
        } else if (mode.equals("type")) {
            for (int i = 3; i < a.length; i++) System.out.printf("%-15s %s%n", a[i], fmt(query(dict, session, a[i], "<s>"), 6));
        }
    }
}
