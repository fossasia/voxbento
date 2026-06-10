import { useMDXComponents as getMDXComponents } from 'nextra-theme-docs'

export function useMDXComponents(components: any) {
  return {
    ...getMDXComponents(),
    ...components
  }
}