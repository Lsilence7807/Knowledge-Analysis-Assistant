// 文件：frontend/src/routes/login.tsx
// 作用：登录页（P11）：公开端开着认证时，没有 cookie 的请求都会被 401 顶回来，这一页负责换一张
// 阶段：F7 守卫与认证（兼 P11）
// 依赖：react、src/components/ui/{button,card,input,label}.tsx、src/lib/apiClient.ts
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { login } from "@/lib/apiClient";

export function LoginPage() {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await login(password);
      // 整页重载而不是前端跳路由：登录前的请求都带着「未登录」的结果，重来一遍最省心
      window.location.href = "/";
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "登录失败");
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto mt-10 max-w-[420px]">
      <Card className="border-[3px] border-ink shadow-hard">
        <CardHeader>
          <CardTitle className="text-base">登录</CardTitle>
          <CardDescription>公开部署开了单密码认证；本地自用（AUTH_DISABLED=true）不会看到这一页。</CardDescription>
        </CardHeader>
        <CardContent>
          <form className="space-y-3" onSubmit={submit}>
            <div className="space-y-1.5">
              <Label htmlFor="password">密码</Label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            </div>
            <Button type="submit" disabled={busy || !password}>
              {busy ? "登录中…" : "登录"}
            </Button>
            {error ? <p className="text-sm text-destructive">{error}</p> : null}
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
