import { createRouter, createWebHistory } from 'vue-router'
import InterpreterConsoleView from '../views/InterpreterConsoleView.vue'

const routes = [
  {
    path: '/',
    redirect: '/interpreter'
  },
  {
    path: '/interpreter/:eventSlug?/:boothId?',
    name: 'interpreter-console',
    component: InterpreterConsoleView
  }
]

export default createRouter({
  history: createWebHistory(),
  routes
})
