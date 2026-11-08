package com.android.inputmethod.latin.utils;
public class BinaryDictionaryUtils {
  public static native boolean createEmptyDictFileNative(String path, long v, String locale, String[] k, String[] val);
  public static native float calcNormalizedScoreNative(int[] before, int[] after, int score);
  public static native int setCurrentTimeForTestNative(int t);
}
