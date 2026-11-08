package com.android.inputmethod.keyboard;
public class ProximityInfo {
  public static native long setProximityInfoNative(int dw, int dh, int gw, int gh, int kw, int kh, int[] prox, int keyCount, int[] kx, int[] ky, int[] kwid, int[] khei, int[] codes, float[] sx, float[] sy, float[] sr);
  public static native void releaseProximityInfoNative(long p);
}
