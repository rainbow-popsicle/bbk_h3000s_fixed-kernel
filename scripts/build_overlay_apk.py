#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建自动亮度 RRO overlay APK（只需要 aapt2 / apksigner / android.jar）。

为什么需要这些工具
------------------
RRO（运行时资源覆盖）必须是一个**签过名的 APK**，里面带编译后的
resources.arsc。手写二进制 AXML/ARSC 太容易出错，所以走标准工具链。

需要的东西（三样）
------------------
1. aapt2.exe        —— 编译/链接资源      （Android SDK build-tools 里）
2. apksigner.bat    —— 签名               （Android SDK build-tools 里）
3. android.jar      —— 平台资源，链接时用 （Android SDK platforms/<API>/ 里）

自动查找位置（按顺序）：
    %LOCALAPPDATA%\\Android\\Sdk\\build-tools\\*
    %USERPROFILE%\\AppData\\Local\\Android\\Sdk\\build-tools\\*
    C:\\Android\\build-tools\\*  /  D:\\  /  E:\\
也可以用参数显式指定：
    --build-tools <目录>   --android-jar <路径>

签名
----
没有 keytool 也能签：用 Java 直接生成一个自签名 PKCS12 密钥库
（Java 11+ 支持 `java GenKey.java` 单文件运行，本机是 Java 21）。
自签名就够 —— RRO 不要求和平台同签名。

用法
----
    python scripts/build_overlay_apk.py
    python scripts/build_overlay_apk.py --build-tools "C:\\...\\build-tools\\34.0.0" \
                                        --android-jar "C:\\...\\platforms\\android-34\\android.jar"
"""
import argparse
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
PROJ = os.path.join(ROOT, "overlay", "autobrightness")
BUILD = os.path.join(ROOT, "out", "overlay-build")
OUT_APK = os.path.join(ROOT, "out", "s3pro-autobrightness-overlay.apk")
KS = os.path.join(BUILD, "debug.p12")

GENKEY_JAVA = r"""
import java.io.*;
import java.security.*;
import java.security.cert.*;
import java.math.BigInteger;
import java.util.Date;
import javax.security.auth.x500.X500Principal;
import sun.security.x509.*;

public class GenKey {
    public static void main(String[] a) throws Exception {
        String path = a[0];
        KeyPairGenerator kpg = KeyPairGenerator.getInstance("RSA");
        kpg.initialize(2048);
        KeyPair kp = kpg.generateKeyPair();

        X500Principal dn = new X500Principal("CN=S3Pro Overlay, O=local, C=CN");
        X509CertInfo info = new X509CertInfo();
        CertificateValidity validity = new CertificateValidity(
                new Date(System.currentTimeMillis() - 86400000L),
                new Date(System.currentTimeMillis() + 3650L * 86400000L));
        info.set(X509CertInfo.VALIDITY, validity);
        info.set(X509CertInfo.SERIAL_NUMBER,
                 new CertificateSerialNumber(BigInteger.valueOf(System.currentTimeMillis())));
        info.set(X509CertInfo.SUBJECT, dn);
        info.set(X509CertInfo.ISSUER, dn);
        info.set(X509CertInfo.KEY, new CertificateX509Key(kp.getPublic()));
        info.set(X509CertInfo.VERSION, new CertificateVersion(CertificateVersion.V3));
        AlgorithmId algo = new AlgorithmId(AlgorithmId.sha256WithRSAEncryption_oid);
        info.set(X509CertInfo.ALGORITHM_ID, new CertificateAlgorithmId(algo));

        X509CertImpl cert = new X509CertImpl(info);
        cert.sign(kp.getPrivate(), "SHA256withRSA");

        KeyStore ks = KeyStore.getInstance("PKCS12");
        ks.load(null, null);
        ks.setKeyEntry("key", kp.getPrivate(), "android".toCharArray(),
                       new java.security.cert.Certificate[]{cert});
        try (FileOutputStream fos = new FileOutputStream(path)) {
            ks.store(fos, "android".toCharArray());
        }
        System.out.println("keystore written: " + path);
    }
}
"""


def find_tool(names, extra_dirs=()):
    for d in extra_dirs:
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    for n in names:
        w = shutil.which(n)
        if w:
            return w
    return None


def candidate_sdk_roots():
    roots = []
    for env in ("LOCALAPPDATA", "USERPROFILE", "HOME"):
        v = os.environ.get(env)
        if v:
            roots.append(os.path.join(v, "AppData", "Local", "Android", "Sdk"))
            roots.append(os.path.join(v, "Android", "Sdk"))
    roots += [r"C:\Android", r"D:\Android", r"E:\Android",
              r"C:\Android\Sdk", r"D:\Android\Sdk", r"E:\Android\Sdk"]
    return roots


def autodetect():
    """返回 (aapt2, apksigner, android_jar)"""
    aapt2 = apksigner = None
    android_jar = None
    bt_dir = None

    for root in candidate_sdk_roots():
        bt = os.path.join(root, "build-tools")
        if os.path.isdir(bt):
            vers = sorted(os.listdir(bt), reverse=True)
            for v in vers:
                d = os.path.join(bt, v)
                a = find_tool(["aapt2.exe", "aapt2"], [d])
                s = find_tool(["apksigner.bat", "apksigner"], [d])
                if a and s:
                    aapt2, apksigner, bt_dir = a, s, d
                    break
        plat = os.path.join(root, "platforms")
        if os.path.isdir(plat):
            for v in sorted(os.listdir(plat), reverse=True):
                j = os.path.join(plat, v, "android.jar")
                if os.path.isfile(j):
                    android_jar = j
                    break
        if aapt2 and android_jar:
            break
    return aapt2, apksigner, android_jar, bt_dir


def run(cmd, cwd=None):
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=cwd)
    if p.stdout.strip():
        sys.stdout.write(p.stdout)
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        raise SystemExit("命令失败: %s" % " ".join(map(str, cmd)))
    return p


def ensure_keystore():
    if os.path.isfile(KS):
        return KS
    jf = os.path.join(BUILD, "GenKey.java")
    with open(jf, "w", encoding="utf-8") as f:
        f.write(GENKEY_JAVA)
    java = find_tool(["java.exe", "java"])
    if not java:
        raise SystemExit("没有 java，无法生成签名密钥")
    print("--- 生成自签名密钥库 ---")
    run([java, jf, KS])
    return KS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-tools", default=None, help="build-tools 目录")
    ap.add_argument("--android-jar", default=None)
    ap.add_argument("--out", default=OUT_APK)
    args = ap.parse_args()

    aapt2, apksigner, android_jar, bt_dir = autodetect()
    if args.build_tools:
        bt_dir = args.build_tools
        aapt2 = find_tool(["aapt2.exe", "aapt2"], [bt_dir]) or aapt2
        apksigner = find_tool(["apksigner.bat", "apksigner"], [bt_dir]) or apksigner
    if args.android_jar:
        android_jar = args.android_jar

    print("=== 工具检查 ===")
    print("  aapt2       : %s" % (aapt2 or "缺"))
    print("  apksigner   : %s" % (apksigner or "缺"))
    print("  android.jar : %s" % (android_jar or "缺"))
    if not (aapt2 and apksigner and android_jar):
        print()
        print("缺工具。请下载并放到 Downloads 下（见文档），或直接告诉我路径：")
        print("  1) build-tools（含 aapt2.exe / apksigner.bat）")
        print("  2) platforms/android-XX/android.jar")
        return 2

    if os.path.isdir(BUILD):
        shutil.rmtree(BUILD)
    os.makedirs(BUILD, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

    print()
    print("=== 1) 编译资源 ===")
    res = os.path.join(PROJ, "res")
    flat = os.path.join(BUILD, "res.zip")
    run([aapt2, "compile", "--dir", res, "-o", flat])

    print("=== 2) 链接成 APK ===")
    unsigned = os.path.join(BUILD, "unsigned.apk")
    run([aapt2, "link", "-o", unsigned,
         "-I", android_jar,
         "--manifest", os.path.join(PROJ, "AndroidManifest.xml"),
         "--min-sdk-version", "26", "--target-sdk-version", "27",
         flat])

    print("=== 3) 签名 ===")
    ks = ensure_keystore()
    run([apksigner, "sign",
         "--ks", ks, "--ks-type", "PKCS12",
         "--ks-pass", "pass:android", "--key-pass", "pass:android",
         "--min-sdk-version", "26",
         "--out", args.out, unsigned])

    print("=== 4) 校验 ===")
    run([apksigner, "verify", "--print-certs", args.out])
    with zipfile.ZipFile(args.out) as z:
        names = z.namelist()
    print("  APK 内文件: %s" % ", ".join(names))
    print()
    print("  ✓ 产物: %s (%d 字节)" % (args.out, os.path.getsize(args.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
