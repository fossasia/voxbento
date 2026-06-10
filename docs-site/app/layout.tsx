import 'nextra-theme-docs/style.css'

export const metadata = {
  title: 'Voxbento Docs',
  description: 'Voxbento Documentation'
}

export default function RootLayout({
  children
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  )
}