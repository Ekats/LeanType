package com.android.inputmethod.latin;
import java.util.ArrayList;
import com.android.inputmethod.latin.utils.WordInputEventForPersonalization;
public class BinaryDictionary {
  public static native long openNative(String sourceDir, long dictOffset, long dictSize, boolean isUpdatable);
  public static native long createOnMemoryNative(long formatVersion, String locale, String[] k, String[] v);
  public static native void getHeaderInfoNative(long dict, int[] a, int[] b, ArrayList<int[]> c, ArrayList<int[]> d);
  public static native boolean flushNative(long dict, String filePath);
  public static native boolean needsToRunGCNative(long dict, boolean m);
  public static native boolean flushWithGCNative(long dict, String filePath);
  public static native void closeNative(long dict);
  public static native int getFormatVersionNative(long dict);
  public static native int getProbabilityNative(long dict, int[] word);
  public static native int getMaxProbabilityOfExactMatchesNative(long dict, int[] word);
  public static native int getNgramProbabilityNative(long dict, int[][] prev, boolean[] bos, int[] word);
  public static native void getWordPropertyNative(long dict, int[] word, boolean bos, int[] outCp, boolean[] outFlags, int[] outProb, ArrayList<int[][]> a, ArrayList<boolean[]> b, ArrayList<int[]> c, ArrayList<int[]> d, ArrayList<int[]> e, ArrayList<Integer> f);
  public static native int getNextWordNative(long dict, int token, int[] outCp, boolean[] outBos);
  public static native void getSuggestionsNative(long dict, long proximityInfo, long traverseSession, int[] x, int[] y, int[] times, int[] pointerIds, int[] inputCodePoints, int inputSize, int[] suggestOptions, int[][] prev, boolean[] bos, int prevWordCount, int[] outCount, int[] outCp, int[] outScores, int[] outIndices, int[] outTypes, int[] outConf, float[] weight);
  public static native boolean addUnigramEntryNative(long dict, int[] word, int probability, int[] shortcutTarget, int shortcutProbability, boolean bos, boolean notAWord, boolean offensive, int timestamp);
  public static native boolean removeUnigramEntryNative(long dict, int[] word);
  public static native boolean addNgramEntryNative(long dict, int[][] prev, boolean[] bos, int[] word, int probability, int timestamp);
  public static native boolean removeNgramEntryNative(long dict, int[][] prev, boolean[] bos, int[] word);
  public static native boolean updateEntriesForWordWithNgramContextNative(long dict, int[][] prev, boolean[] bos, int[] word, boolean valid, int count, int timestamp);
  public static native int updateEntriesForInputEventsNative(long dict, WordInputEventForPersonalization[] ev, int start);
  public static native String getPropertyNative(long dict, String q);
  public static native boolean isCorruptedNative(long dict);
  public static native boolean migrateNative(long dict, String path, long v);
}
