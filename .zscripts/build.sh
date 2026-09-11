#!/bin/bash

# 将 stderr 重定向到 stdout，避免 execute_command 因为 stderr 输出而报错
exec 2>&1

set -e

# 获取脚本所在目录（.zscripts 目录，即 workspace-agent/.zscripts）
# 使用 $0 获取脚本路径（兼容 sh 和 bash）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 前端项目路径（Vite 构建，产物为纯静态 dist/）
PROJECT_DIR="/home/z/my-project"

# 检查项目目录是否存在
if [ ! -d "$PROJECT_DIR" ]; then
    echo "❌ 错误: 项目目录不存在: $PROJECT_DIR"
    exit 1
fi

echo "🚀 开始构建前端应用和 mini-services..."
echo "📁 项目路径: $PROJECT_DIR"

# 切换到项目目录
cd "$PROJECT_DIR" || exit 1

BUILD_DIR="/tmp/build_fullstack_$BUILD_ID"
echo "📁 清理并创建构建目录: $BUILD_DIR"
mkdir -p "$BUILD_DIR"

# 安装依赖
echo "📦 安装依赖..."
bun install

# 构建前端应用（tsc --noEmit + vite build，纯静态产物 dist/）
echo "🔨 构建前端应用..."
bun run build

# 校验静态入口是否生成（部署成功率守卫）。
if [ ! -f "dist/index.html" ]; then
    echo "❌ 构建失败：未生成 dist/index.html，请检查上方构建日志中的报错。"
    exit 1
fi
echo "✅ 前端静态构建完成。"

# 构建 mini-services
# 检查 Next.js 项目目录下是否有 mini-services 目录
if [ -d "$PROJECT_DIR/mini-services" ]; then
    echo "🔨 构建 mini-services..."
    # 使用 workspace-agent 目录下的 mini-services 脚本
    sh "$SCRIPT_DIR/mini-services-install.sh"
    sh "$SCRIPT_DIR/mini-services-build.sh"

    # 复制 mini-services-start.sh 到 mini-services-dist 目录
    echo "  - 复制 mini-services-start.sh 到 $BUILD_DIR"
    cp "$SCRIPT_DIR/mini-services-start.sh" "$BUILD_DIR/mini-services-start.sh"
    chmod +x "$BUILD_DIR/mini-services-start.sh"
else
    echo "ℹ️  mini-services 目录不存在，跳过"
fi

# 将所有构建产物复制到临时构建目录
echo "📦 收集构建产物到 $BUILD_DIR..."

# 复制前端静态构建产物（Vite dist/，Caddy 在部署容器内直接托管）
if [ -d "dist" ]; then
    echo "  - 复制 dist/ → web-dist/"
    cp -r dist "$BUILD_DIR/web-dist"
else
    echo "❌ 未找到前端构建产物 dist/"
    exit 1
fi

PROJECT_DIR="$PROJECT_DIR" BUILD_DIR="$BUILD_DIR" \
    bash "$SCRIPT_DIR/python-runtime-build.sh"

# 生成生产环境 Caddyfile：前端是纯静态构建，由 Caddy 直接托管（部署容器内
# 不再有任何 :3000 Node 服务进程）；XTransformPort 查询仍反代到对应
# mini-service 端口。开发环境的仓库 Caddyfile 保持反代 :3000 不变。
echo "  - 生成生产 Caddyfile"
cat > "$BUILD_DIR/Caddyfile" <<'EOF'
:81 {
	@transform_port_query {
		query XTransformPort=*
	}

	handle @transform_port_query {
		reverse_proxy localhost:{query.XTransformPort} {
			header_up Host {host}
			header_up X-Forwarded-For {remote_host}
			header_up X-Forwarded-Proto {scheme}
			header_up X-Real-IP {remote_host}
		}
	}

	handle {
		root * /app/web-dist
		try_files {path} /index.html
		file_server
	}
}
EOF

# 复制 start.sh 脚本
echo "  - 复制 start.sh 到 $BUILD_DIR"
cp "$SCRIPT_DIR/start.sh" "$BUILD_DIR/start.sh"
chmod +x "$BUILD_DIR/start.sh"

# 打包到 $BUILD_DIR.tar.gz
PACKAGE_FILE="${BUILD_DIR}.tar.gz"
echo ""
echo "📦 打包构建产物到 $PACKAGE_FILE..."
cd "$BUILD_DIR" || exit 1
tar -czf "$PACKAGE_FILE" .
cd - > /dev/null || exit 1

# # 清理临时目录
# rm -rf "$BUILD_DIR"

echo ""
echo "✅ 构建完成！所有产物已打包到 $PACKAGE_FILE"
echo "📊 打包文件大小:"
ls -lh "$PACKAGE_FILE"
