package com.android.inputmethod.latin;
public class DicTraverseSession {
  public static native long setDicTraverseSessionNative(String locale, long dictSize);
  public static native void initDicTraverseSessionNative(long s, long dict, int[] prev, int len);
  public static native void releaseDicTraverseSessionNative(long s);
}
