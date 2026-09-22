// 文件：frontend/src/components/decor.tsx
// 作用：装饰层——eyebrow 小标签、圆形贴纸、旋转便签、色块圆点、插画引用；只做视觉，不带数据与路由逻辑
// 阶段：P5.3 视觉风格 v2
// 依赖：react、cn
import { cn } from "cn";
import type { ReactNode } from "react";

// 底色一律取 P5.1 旋钮映射出来的工具类，不引第二套配色
const TONE = {
  violet: "bg-violet text-primary-foreground",
  yellow: "bg-yellow text-ink",
  coral: "bg-coral text-primary-foreground",
  mint: "bg-mint text-primary-foreground",
  card: "bg-card text-ink",
} as const;

const BLOB = {
  violet: "bg-violet",
  yellow: "bg-yellow",
  coral: "bg-coral",
  mint: "bg-mint",
} as const;

export type Tone = keyof typeof TONE;
export type BlobTone = keyof typeof BLOB;

export function Eyebrow({ children, className }: { children: ReactNode; className?: string }) {
  return <p className={cn("text-xs font-extrabold tracking-[0.22em] text-violet-dark", className)}>{children}</p>;
}

export function Sticker({
  children,
  tone = "mint",
  className,
}: {
  children: ReactNode;
  tone?: Tone;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "grid size-20 shrink-0 place-items-center rounded-full border-[3px] border-ink text-center text-[11px] leading-tight font-extrabold shadow-hard-sm",
        TONE[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

export function TiltNote({
  title,
  children,
  tone = "yellow",
  tilt = "left",
  className,
}: {
  title: string;
  children: ReactNode;
  tone?: Tone;
  tilt?: "left" | "right";
  className?: string;
}) {
  return (
    <div
      className={cn(
        "w-[228px] rounded-2xl border-[3px] border-ink p-3 shadow-hard",
        tilt === "left" ? "rotate-[-4deg]" : "rotate-[4deg]",
        TONE[tone],
        className,
      )}
    >
      <p className="text-[11px] font-extrabold tracking-[0.18em] opacity-75">{title}</p>
      <p className="mt-1 text-sm leading-snug font-bold">{children}</p>
    </div>
  );
}

export function Blob({ className, tone = "mint" }: { className?: string; tone?: BlobTone }) {
  return <span aria-hidden="true" className={cn("pointer-events-none absolute rounded-full opacity-70", BLOB[tone], className)} />;
}

export function Illustration({ name, alt, className }: { name: string; alt: string; className?: string }) {
  return (
    <img
      src={`/illustrations/${name}.svg`}
      alt={alt}
      draggable={false}
      className={cn("h-auto select-none", className)}
    />
  );
}
