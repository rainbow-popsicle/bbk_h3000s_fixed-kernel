/*
 * GenCert.java —— 只用 Java 公共 API 生成 RSA 密钥对 + 自签名 X.509 证书
 *
 * 为什么需要它：
 *   apksigner 签名必须提供密钥和证书，但本机没有 keytool（Oracle 的
 *   javapath 里只有 java/javac），也没装 openssl/BouncyCastle/Python 加密库，
 *   而 Java 21 又封了 sun.security.x509 那套内部 API（用它编译会报
 *   "找不到符号"）。
 *
 * 做法：
 *   · RSA 密钥对 —— KeyPairGenerator（公共 API）
 *   · 签名      —— Signature("SHA256withRSA")（公共 API，自带 PKCS#1 v1.5 填充）
 *   · 私钥导出  —— privateKey.getEncoded() 返回的就是 PKCS#8 DER，
 *                  正好是 apksigner --key 要的 .pk8 格式
 *   · X.509 外壳 —— 手写 DER 编码（只有 serial/算法/名称/有效期/SPKI 五段，
 *                  不需要任何扩展，v3 的 version 字段填 2）
 *
 * 单文件运行（Java 11+）：
 *   java GenCert.java <输出目录>
 * 产物：
 *   <输出目录>/key.pk8    PKCS#8 DER 私钥
 *   <输出目录>/cert.pem   X.509 自签名证书（PEM）
 */
import java.io.*;
import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.security.*;
import java.security.spec.X509EncodedKeySpec;
import java.text.SimpleDateFormat;
import java.util.*;

public class GenCert {

    /* ---------------- 极简 DER 编码器 ---------------- */

    static byte[] tlv(int tag, byte[] content) {
        ByteArrayOutputStream o = new ByteArrayOutputStream();
        o.write(tag);
        int len = content.length;
        if (len < 0x80) {
            o.write(len);
        } else if (len < 0x100) {
            o.write(0x81); o.write(len);
        } else if (len < 0x10000) {
            o.write(0x82); o.write(len >> 8); o.write(len & 0xff);
        } else {
            o.write(0x83); o.write(len >> 16);
            o.write((len >> 8) & 0xff); o.write(len & 0xff);
        }
        o.write(content, 0, content.length);
        return o.toByteArray();
    }

    static byte[] seq(byte[]... parts) {
        ByteArrayOutputStream o = new ByteArrayOutputStream();
        for (byte[] p : parts) o.write(p, 0, p.length);
        return tlv(0x30, o.toByteArray());
    }

    static byte[] set(byte[]... parts) {
        ByteArrayOutputStream o = new ByteArrayOutputStream();
        for (byte[] p : parts) o.write(p, 0, p.length);
        return tlv(0x31, o.toByteArray());
    }

    static byte[] integer(BigInteger v) {
        return tlv(0x02, v.toByteArray());
    }

    static byte[] oid(String s) {
        String[] p = s.split("\\.");
        ByteArrayOutputStream o = new ByteArrayOutputStream();
        o.write(Integer.parseInt(p[0]) * 40 + Integer.parseInt(p[1]));
        for (int i = 2; i < p.length; i++) {
            long v = Long.parseLong(p[i]);
            if (v < 0x80) {
                o.write((int) v);
            } else {
                // base-128，高位续
                int n = 0;
                long t = v;
                while (t > 0) { t >>= 7; n++; }
                for (int k = n - 1; k >= 0; k--) {
                    int b = (int) ((v >> (k * 7)) & 0x7f);
                    if (k != 0) b |= 0x80;
                    o.write(b);
                }
            }
        }
        return tlv(0x06, o.toByteArray());
    }

    static byte[] utf8(String s) {
        return tlv(0x0C, s.getBytes(StandardCharsets.UTF_8));
    }

    static byte[] utcTime(Date d) {
        SimpleDateFormat f = new SimpleDateFormat("yyMMddHHmmss'Z'", Locale.US);
        f.setTimeZone(TimeZone.getTimeZone("UTC"));
        return tlv(0x17, f.format(d).getBytes(StandardCharsets.US_ASCII));
    }

    static byte[] bitString(byte[] b) {
        byte[] c = new byte[b.length + 1];
        c[0] = 0;                       // 0 个未用位
        System.arraycopy(b, 0, c, 1, b.length);
        return tlv(0x03, c);
    }

    static byte[] nameRdn(String cn) {
        // SEQUENCE { SET { SEQUENCE { OID 2.5.4.3, UTF8String cn } } }
        return seq(set(seq(oid("2.5.4.3"), utf8(cn))));
    }

    public static void main(String[] args) throws Exception {
        String outDir = args.length > 0 ? args[0] : ".";
        new File(outDir).mkdirs();

        KeyPairGenerator kpg = KeyPairGenerator.getInstance("RSA");
        kpg.initialize(2048);
        KeyPair kp = kpg.generateKeyPair();

        // sha256WithRSAEncryption = 1.2.840.113549.1.1.11
        byte[] alg = seq(oid("1.2.840.113549.1.1.11"), tlv(0x05, new byte[0]));

        BigInteger serial = BigInteger.valueOf(System.currentTimeMillis());
        Date notBefore = new Date(System.currentTimeMillis() - 86400000L);
        Date notAfter = new Date(System.currentTimeMillis() + 3650L * 86400000L);

        byte[] spki = kp.getPublic().getEncoded();   // X.509 SubjectPublicKeyInfo DER
        byte[] subj = nameRdn("S3Pro Overlay");

        byte[] tbs = seq(
                tlv(0xA0, integer(BigInteger.valueOf(2))),   // [0] version v3
                integer(serial),
                alg,
                subj,                                        // issuer = subject（自签名）
                seq(utcTime(notBefore), utcTime(notAfter)),
                subj,
                spki);

        Signature sig = Signature.getInstance("SHA256withRSA");
        sig.initSign(kp.getPrivate());
        sig.update(tbs);
        byte[] signature = sig.sign();

        byte[] cert = seq(tbs, alg, bitString(signature));

        // 私钥：Java 的 getEncoded() 就是 PKCS#8 DER
        try (FileOutputStream f = new FileOutputStream(new File(outDir, "key.pk8"))) {
            f.write(kp.getPrivate().getEncoded());
        }

        // 证书：DER → PEM
        String b64 = Base64.getMimeEncoder(64, "\n".getBytes()).encodeToString(cert);
        try (Writer w = new OutputStreamWriter(
                new FileOutputStream(new File(outDir, "cert.pem")), StandardCharsets.US_ASCII)) {
            w.write("-----BEGIN CERTIFICATE-----\n");
            w.write(b64);
            w.write("\n-----END CERTIFICATE-----\n");
        }

        System.out.println("key.pk8  : " + new File(outDir, "key.pk8").length() + " bytes");
        System.out.println("cert.pem : " + new File(outDir, "cert.pem").length() + " bytes");
        System.out.println("serial   : " + serial.toString(16));
    }
}
